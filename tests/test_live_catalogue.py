"""Model-independent live dataset registration, mapping and ownership."""

import copy
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import scaled
from live_helpers import Rollouts

from levi import children, paths
from levi.dataset_management import ManagementError
from levi.live import catalogue, cli, controller, jsonio, mirror, resources
from levi.live import config as live_config

NAME = "pi05_fake__stack_the_plates"
PROJECT = Path(__file__).resolve().parents[1]


def config(tmp_path, *, background=False):
    c = live_config.Config()
    c.service.workspace = str(tmp_path / "ws")
    c.service.home = str(tmp_path / "home")
    c.service.heartbeat_s = 0.5
    c.watch.roots = [str(tmp_path / "rollouts")]
    c.watch.backlog = "process"
    c.watch.settle_s = 0.0
    c.gpu.mode = "manual"
    c.gpu.lock_file = ""
    c.pipeline.background = background
    c.resources.threads = 1
    c.resources.view_workers = 1
    cli.prepare(c)
    return c


def run_catalogue(c):
    target = c.live_dir / "effective.toml"
    target.write_text(live_config.render(c))
    env = resources.service_env(c)
    env["LEVI_CPU_ONLY"] = "1"
    # subprocesses must use the worktree code, not the shared venv's package.
    env["PYTHONPATH"] = os.pathsep.join([str(PROJECT), env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [sys.executable, "-m", "levi.live.catalogue", "--config", str(target)],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=scaled(120),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return jsonio.read(c.workspace / "outputs/LEVI/workbench/datasets.json", {})


def intake(c):
    scanner = mirror.Scanner(c)
    for scan in scanner.scan():
        mirror.mirror_dataset(c, mirror.load_state(c, scan.name), scan.ready)


def test_background_off_without_core_registers_all_tasks(
    tmp_path, demo_template, monkeypatch
):
    c = config(tmp_path)
    expected = []
    for task in ("pick_watermelon", "place_on_bread", "move_bread"):
        rollouts = Rollouts(tmp_path / "rollouts", demo_template, task=task)
        rollouts.write(7, run_id="older")
        rollouts.write(9, run_id="newer")
        rollouts.begin(10)
        expected.append(f"pi05_fake__{task}")
    ctl = controller.Controller(
        c,
        probes=controller.Probes(
            vram=lambda: None, ports=set, holders=list, policy_vram=lambda _: None
        ),
        log=lambda *args: None,
    )
    monkeypatch.setattr(ctl, "_reports", lambda _: None)
    monkeypatch.setattr(
        ctl,
        "_spawn",
        lambda *args: (_ for _ in ()).throw(AssertionError("no model worker")),
    )
    try:
        ctl.run(once=True, max_seconds=scaled(120))
        assert ctl.worker is None and not ctl.vllm.mine()
        for name in expected:
            state = mirror.load_state(c, name)
            assert set(state["demos"]) == {"demo_0007", "demo_0009"}
            assert all(row["state"] == "done" for row in state["demos"].values())
            assert state["catalogue"]["view_status"] == "ready"
            assert catalogue.browse_info(c, name, "newer") == {
                "repo_id": f"local/{name}",
                "view_status": "ready",
                "first_episode_index": 1,
            }
            assert (
                catalogue.browse_info(c, name, "unknown")["first_episode_index"] is None
            )
        assert ctl.catalogue is None
    finally:
        ctl.shutdown()
    tracked = children.listed(state=c.workspace / "outputs/LEVI/workbench")
    assert tracked == []


def test_existing_done_mirrors_are_repaired_and_rekeyed_without_changing_labels(
    tmp_path, demo_template
):
    c = config(tmp_path)
    rollouts = Rollouts(tmp_path / "rollouts", demo_template)
    rollouts.write(2, run_id="original-session")
    intake(c)
    operator = {"outcome": "failure", "by": "operator"}
    verdict = {"outcome": "success", "source": "online"}
    relayed = {"status": "ok", "verdict": verdict}

    def label(state):
        state["demos"]["demo_0002"].update(
            state="done", operator_label=operator, verdict=verdict, online=relayed
        )
        return state

    jsonio.update(mirror.state_path(c, NAME), label)
    entry = run_catalogue(c)[NAME]
    first_job = entry["view_job"]
    assert entry["view_status"] == "ready"
    assert mirror.load_state(c, NAME)["demos"]["demo_0002"]["episode_index"] == 0
    assert catalogue.names(c) == []
    assert run_catalogue(c)[NAME]["view_job"] == first_job

    # A newly found earlier demo shifts the published indices; number 2 is
    # not episode 2. The independent intake must update the old done row too.
    rollouts.write(0, run_id="new-session")
    intake(c)
    entry = run_catalogue(c)[NAME]
    assert entry["view_job"] != first_job
    state = mirror.load_state(c, NAME)
    assert state["demos"]["demo_0000"]["episode_index"] == 0
    row = state["demos"]["demo_0002"]
    assert row["episode_index"] == 1 and row["state"] == "done"
    assert (row["operator_label"], row["verdict"], row["online"]) == (
        operator,
        verdict,
        relayed,
    )
    assert (
        catalogue.browse_info(c, NAME, "original-session")["first_episode_index"] == 1
    )
    assert not children.listed(state=c.workspace / "outputs/LEVI/workbench")


def test_mapping_clears_stale_or_deleted_indices_and_preserves_labels(tmp_path):
    c = config(tmp_path)
    state = mirror.empty_state(
        c, (c.watch.roots[0], "pi05_fake", "stack_the_plates"), 0
    )
    state["demos"] = {
        "demo_0004": {
            "state": "done",
            "episode_index": 0,
            "operator_label": {"outcome": "failure"},
            "online": {"status": "ok"},
        },
        "demo_0007": {"state": "done", "episode_index": 1, "deleted": {"at": 1}},
        "demo_0010": {
            "state": "done",
            "episode_index": 2,
            "verdict": {"outcome": "success"},
        },
    }
    before = copy.deepcopy(state["demos"])
    jsonio.write(mirror.state_path(c, NAME), state)
    view = c.workspace / "view"
    (view / "meta").mkdir(parents=True)
    jsonio.write(view / "meta/info.json", {"fps": 10})
    (view / "meta/episodes.jsonl").write_text(
        json.dumps({"episode_index": 5, "source_demo": "task/demo_0004", "length": 11})
        + "\n"
    )
    entry = {
        "id": f"local/{NAME}",
        "name": NAME,
        "view": str(view),
        "view_status": "ready",
    }
    index, lengths, _ = catalogue.map_view(c, NAME, entry)
    assert index == {"demo_0004": 5} and lengths == {"demo_0004": 1}
    rows = mirror.load_state(c, NAME)["demos"]
    assert rows["demo_0004"]["episode_index"] == 5
    assert (
        "episode_index" not in rows["demo_0007"]
        and "episode_index" not in rows["demo_0010"]
    )
    for demo in before:
        assert {k: v for k, v in rows[demo].items() if k != "episode_index"} == {
            k: v for k, v in before[demo].items() if k != "episode_index"
        }


def test_worker_queue_and_reconciliation_share_the_removal_lock(tmp_path, monkeypatch):
    c = config(tmp_path, background=True)
    state = mirror.empty_state(
        c, (c.watch.roots[0], "pi05_fake", "stack_the_plates"), 0
    )
    Path(state["capture"]).mkdir(parents=True)
    state["demos"] = {"demo_0001": {"state": "mirrored"}}
    jsonio.write(mirror.state_path(c, NAME), state)
    ctl = controller.Controller(c, log=lambda *args: None)
    called = []
    monkeypatch.setattr(
        catalogue, "ensure_dataset", lambda *args, **kwargs: called.append(args[1])
    )
    try:
        assert ctl._queue(time.time()) == [NAME]
        with catalogue.dataset_lock(c, NAME) as acquired:
            assert acquired
            assert ctl._queue(time.time()) == []
            assert catalogue.reconcile(c) == 0
            assert called == []
        assert catalogue.reconcile(c) == 0
        assert called == [NAME]
        jsonio.update(mirror.state_path(c, NAME), lambda v: v.update(archived=True))
        assert catalogue.names(c) == []
        assert ctl._queue(time.time()) == []
        assert catalogue.browse_info(c, NAME)["first_episode_index"] is None
    finally:
        ctl.shutdown()


def test_current_and_deleted_rows_do_not_start_catalogue_work(tmp_path):
    c = config(tmp_path)
    state = mirror.empty_state(
        c, (c.watch.roots[0], "pi05_fake", "stack_the_plates"), 0
    )
    Path(state["capture"]).mkdir(parents=True)
    state["demos"] = {"demo_0001": {"state": "mirrored", "deleted": True}}
    jsonio.write(mirror.state_path(c, NAME), state)
    assert catalogue.names(c) == []
    jsonio.update(
        mirror.state_path(c, NAME),
        lambda v: v.update(
            demos={"demo_0001": {"state": "done"}}, current={"demos": ["demo_0001"]}
        ),
    )
    assert catalogue.names(c) == []


def test_children_explicit_workspace_never_writes_the_default_state(
    tmp_path, monkeypatch
):
    default = tmp_path / "default"
    explicit = tmp_path / "live/outputs/LEVI/workbench"
    monkeypatch.setattr(paths, "STATE", default)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        row = children.track(proc, "live-catalogue", state=explicit)
        assert row and children.listed(state=explicit)[0]["running"]
        assert not (default / "processes.json").exists()
        children.untrack(proc.pid, state=explicit)
        proc.wait(timeout=scaled(10))
        assert children.listed(state=explicit) == []
        assert children.reclaim(state=explicit) == []
        assert children.stop_owned(state=explicit) == []
        assert not (default / "processes.json").exists()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=scaled(10))


def test_background_on_intake_is_viewable_while_no_model_worker_can_run(
    tmp_path, demo_template
):
    c = config(tmp_path, background=True)
    rollouts = Rollouts(tmp_path / "rollouts", demo_template)
    rollouts.write(0, run_id="session")
    ctl = controller.Controller(
        c,
        probes=controller.Probes(
            vram=lambda: None, ports=set, holders=list, policy_vram=lambda _: None
        ),
        log=lambda *args: None,
    )
    try:
        ctl._refresh(time.time(), True)
        ctl._take_in(time.time())
        assert mirror.load_state(c, NAME)["demos"]["demo_0000"]["state"] == "mirrored"
        assert ctl.worker is None and not ctl.vllm.mine()
        run_catalogue(c)
        assert catalogue.browse_info(c, NAME, "session")["first_episode_index"] == 0
    finally:
        ctl.shutdown()


def test_prepare_requests_coalesce_and_are_reaped(tmp_path, demo_template):
    c = config(tmp_path)
    Rollouts(tmp_path / "rollouts", demo_template).write(0)
    intake(c)
    first = catalogue.start(c)
    second = catalogue.start(c)
    assert first["started"] and first["running"]
    assert second == {"pid": first["pid"], "running": True, "started": False}
    deadline = time.monotonic() + scaled(120)
    try:
        while (
            children.identity(first["pid"]) is not None and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert children.identity(first["pid"]) is None
        assert catalogue.browse_info(c, NAME)["view_status"] == "ready"
        while (
            children.listed(state=c.workspace / "outputs/LEVI/workbench")
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert children.listed(state=c.workspace / "outputs/LEVI/workbench") == []
    finally:
        children.stop_owned(state=c.workspace / "outputs/LEVI/workbench")


def test_native_dataset_is_registered_without_a_view_job(tmp_path):
    c = config(tmp_path)
    state = mirror.empty_state(
        c, (c.watch.roots[0], "pi05_fake", "stack_the_plates"), 0
    )
    root = Path(state["capture"])
    (root / "meta").mkdir(parents=True)
    jsonio.write(
        root / "meta/info.json",
        {
            "codebase_version": "v2.1",
            "fps": 10,
            "features": {"state": {"dtype": "float32", "shape": [1]}},
        },
    )
    (root / "meta/episodes.jsonl").write_text(
        json.dumps({"episode_index": 4, "source_demo": "demo_0009", "length": 11})
        + "\n"
    )
    state["demos"] = {"demo_0009": {"state": "done", "run_id": "native"}}
    jsonio.write(mirror.state_path(c, NAME), state)
    entry = run_catalogue(c)[NAME]
    assert entry["kind"] == "lerobot" and not entry.get("view_job")
    assert catalogue.browse_info(c, NAME, "native")["first_episode_index"] == 4


def test_reclaim_marks_only_orphaned_view_jobs_interrupted(tmp_path, monkeypatch):
    c = config(tmp_path)
    state_dir = c.workspace / "outputs/LEVI/workbench"
    jsonio.write(state_dir / "jobs/orphan.json", {"status": "running"})
    jsonio.write(state_dir / "jobs/live.json", {"status": "running"})
    rows = [
        {"kind": "conversion", "label": "orphan", "owner_running": False},
        {"kind": "conversion", "label": "live", "owner_running": True},
    ]
    monkeypatch.setattr(children, "listed", lambda **_: rows)
    monkeypatch.setattr(children, "reclaim", lambda **_: [])
    catalogue._reclaim_jobs(c)
    assert jsonio.read(state_dir / "jobs/orphan.json")["status"] == "interrupted"
    assert jsonio.read(state_dir / "jobs/live.json")["status"] == "running"


def old_unstarted_batch(c, demo_template):
    Rollouts(Path(c.watch.roots[0]), demo_template).write(0, run_id="paused-session")
    intake(c)
    batch = {
        "demos": ["demo_0000"],
        "started_at": 1,
        "temporal": {},
        "anchored": None,
        "done": [],
    }
    jsonio.update(
        mirror.state_path(c, NAME), lambda v: v.update(current=copy.deepcopy(batch))
    )
    old = subprocess.Popen([sys.executable, "-c", "pass"])
    old.wait(timeout=scaled(10))
    jsonio.write(
        c.live_dir / "worker.json",
        {"pid": old.pid, "dataset": NAME, "phase": "exited", "exit": 10},
    )
    return batch


def test_old_need_model_batch_can_prepare_without_losing_the_batch(
    tmp_path, demo_template
):
    c = config(tmp_path)
    batch = old_unstarted_batch(c, demo_template)
    assert catalogue.names(c) == [NAME]
    with catalogue.dataset_lock(c, NAME) as acquired:
        assert acquired
        assert (
            catalogue.reconcile(c) == 0
        )  # The preparation cannot bypass a live owner.
        assert catalogue.browse_info(c, NAME)["view_status"] == "missing"
    entry = run_catalogue(c)[NAME]
    state = mirror.load_state(c, NAME)
    assert state["current"] == batch
    assert state["demos"]["demo_0000"]["state"] == "mirrored"
    assert state["demos"]["demo_0000"]["episode_index"] == 0
    assert catalogue.browse_info(c, NAME, "paused-session")["first_episode_index"] == 0
    assert entry["view_status"] == "ready"
    assert catalogue.names(c) == []


@pytest.mark.parametrize(
    "guard", ["temporal", "anchored", "done", "alive", "missing", "wrong_dataset"]
)
def test_preparation_refuses_current_with_references_or_unverified_worker(
    tmp_path, demo_template, guard
):
    c = config(tmp_path)
    batch = old_unstarted_batch(c, demo_template)
    if guard in ("temporal", "anchored", "done"):
        batch[guard] = (
            {"run_id": "already-planned"} if guard != "done" else ["demo_0000"]
        )
        jsonio.update(mirror.state_path(c, NAME), lambda v: v.update(current=batch))
    elif guard == "alive":
        jsonio.update(c.live_dir / "worker.json", lambda v: v.update(pid=os.getpid()))
    elif guard == "missing":
        (c.live_dir / "worker.json").unlink()
    else:
        jsonio.update(
            c.live_dir / "worker.json", lambda v: v.update(dataset="another-dataset")
        )
    assert catalogue.names(c) == []
    assert run_catalogue(c) == {}
    with pytest.raises(ManagementError) as refused:
        catalogue.prepare(c, NAME)
    assert refused.value.status_code == 409
    assert mirror.load_state(c, NAME)["current"] == batch


@pytest.mark.parametrize(
    "activity", ["conversion", "running_run", "paused_run", "unreadable"]
)
def test_old_batch_is_not_prepared_while_other_records_reference_its_indices(
    tmp_path, demo_template, activity
):
    c = config(tmp_path)
    batch = old_unstarted_batch(c, demo_template)
    state_dir = c.workspace / "outputs/LEVI/workbench"
    if activity == "conversion":
        jsonio.write(
            state_dir / "jobs/active.json",
            {"status": "running", "source": str(c.captures_dir / NAME)},
        )
    elif activity == "unreadable":
        (state_dir / "jobs").mkdir(parents=True)
        (state_dir / "jobs/broken.json").write_text("invalid json")
    else:
        (state_dir / "agent").mkdir(parents=True)
        with sqlite3.connect(state_dir / "agent/workbench.sqlite3") as db:
            db.execute("CREATE TABLE records (kind TEXT, body TEXT)")
            db.execute(
                "INSERT INTO records VALUES (?, ?)",
                (
                    "runs",
                    json.dumps(
                        {
                            "status": "running"
                            if activity == "running_run"
                            else "paused",
                            "context": {"repo_id": f"local/{NAME}", "episodes": [0]},
                        }
                    ),
                ),
            )
    # The lightweight candidate check is repeated with job evidence after
    # taking the management lock; no view job or state reset is allowed.
    assert catalogue.names(c) == [NAME]
    assert run_catalogue(c) == {}
    assert mirror.load_state(c, NAME)["current"] == batch
    assert catalogue.browse_info(c, NAME)["view_status"] == "missing"


def test_worker_builds_view_before_exiting_for_a_missing_model(tmp_path, demo_template):
    c = config(tmp_path, background=True)
    c.pipeline.temporal = False
    Rollouts(Path(c.watch.roots[0]), demo_template).write(0)
    intake(c)
    target = c.live_dir / "effective.toml"
    target.write_text(live_config.render(c))
    env = resources.service_env(c)
    env["LEVI_CPU_ONLY"] = "1"
    env["PYTHONPATH"] = os.pathsep.join([str(PROJECT), env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "levi.live.worker",
            "--config",
            str(target),
            "--dataset",
            NAME,
            "--provider-json",
            json.dumps({"name": "absent", "port": 0}),
        ],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=scaled(120),
    )
    assert result.returncode == 10, result.stdout + result.stderr
    state = mirror.load_state(c, NAME)
    assert state["current"]["demos"] == ["demo_0000"]
    assert state["current"]["temporal"] == {} and state["current"]["anchored"] is None
    assert catalogue.browse_info(c, NAME)["view_status"] == "ready"
    assert state["demos"]["demo_0000"]["episode_index"] == 0


@pytest.mark.parametrize(
    "activity", ["paused", "running", "namespace_job", "overlay_plan", "overlay_rows"]
)
def test_namespaced_work_and_unpublished_overlays_prevent_view_preparation(
    tmp_path, demo_template, activity
):
    c = config(tmp_path)
    batch = old_unstarted_batch(c, demo_template)
    state_dir = c.workspace / "outputs/LEVI/workbench"
    base, namespace = "registered-capture", "registered-capture--review"
    catalog_items = {
        base: {"name": base, "path": str(c.captures_dir / NAME), "kind": "raw"},
        namespace: {
            "name": namespace,
            "base": base,
            "path": str(c.captures_dir / NAME),
        },
    }
    jsonio.write(state_dir / "datasets.json", catalog_items)
    if activity in ("paused", "running"):
        (state_dir / "agent").mkdir(parents=True)
        with sqlite3.connect(state_dir / "agent/workbench.sqlite3") as db:
            db.execute("CREATE TABLE records (kind TEXT, body TEXT)")
            db.execute(
                "INSERT INTO records VALUES (?, ?)",
                (
                    "runs",
                    json.dumps(
                        {
                            "status": activity,
                            "context": {"repo_id": f"local/{namespace}"},
                        }
                    ),
                ),
            )
    elif activity == "namespace_job":
        jsonio.write(
            state_dir / "segmentation" / namespace / "jobs/segment.retry.json",
            {"status": "running"},
        )
    else:
        folder = state_dir / "segmentation" / namespace / "live/overlay"
        jsonio.write(folder / "plan.json", {"episode_index": 0})
        if activity == "overlay_rows":
            jsonio.write(folder / "result.json", {})
            (folder / "rows").mkdir()
            (folder / "rows/camera.jsonl").write_text("{}\n")
    state = mirror.load_state(c, NAME)
    assert not catalogue._jobs_idle(c, NAME, state)
    assert run_catalogue(c) == catalog_items
    with pytest.raises(ManagementError) as refused:
        catalogue.prepare(c, NAME)
    assert refused.value.status_code == 409
    assert mirror.load_state(c, NAME)["current"] == batch
    assert not (state_dir / "jobs").exists()


def test_job_and_overlay_safety_check_imports_no_numeric_or_model_modules(
    tmp_path, demo_template
):
    c = config(tmp_path)
    old_unstarted_batch(c, demo_template)
    target = c.live_dir / "effective.toml"
    target.write_text(live_config.render(c))
    script = """import sys
from levi.live import catalogue, config, mirror
c = config.load(sys.argv[1])
assert catalogue._jobs_idle(c, sys.argv[2], mirror.load_state(c, sys.argv[2]))
assert not any(name.split('.')[0] in {'numpy', 'pandas', 'torch', 'transformers'} for name in sys.modules), sorted(sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(target), NAME],
        cwd=PROJECT,
        env=resources.service_env(c),
        capture_output=True,
        text=True,
        check=False,
        timeout=scaled(20),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_once_waits_for_a_pending_batch_temporarily_owned_by_preparation(
    tmp_path, demo_template, monkeypatch
):
    from test_live_pipeline import Env

    e = Env(tmp_path, demo_template)
    e.rollouts.write(0)
    intake(e.config)
    entered, release = threading.Event(), threading.Event()

    def preparing():
        with catalogue.dataset_lock(e.config, NAME) as acquired:
            assert acquired
            entered.set()
            release.wait(scaled(20))

    owner = threading.Thread(target=preparing)
    owner.start()
    assert entered.wait(scaled(5))
    ctl = e.controller()
    # Isolate the external prepare owner; the worker builds its real view
    # after acquiring the lock and annotates through the complete fake VLM.
    monkeypatch.setattr(ctl, "_catalogue_step", lambda now: None)
    timer = threading.Timer(1.0, release.set)
    try:
        ctl.tick()
        assert ctl._queue(time.time()) == []
        assert ctl._queue(time.time(), include_busy=True) == [NAME]
        timer.start()
        ctl.run(once=True, max_seconds=scaled(120))
        assert e.state()["demos"]["demo_0000"]["state"] == "done"
        assert e.atoms(0) and not ctl.nothing and not ctl.failures
    finally:
        release.set()
        owner.join(scaled(5))
        timer.cancel()
        e.close()


def test_a_worker_losing_the_lock_reports_busy_without_backing_off(
    tmp_path, demo_template
):
    c = config(tmp_path, background=True)
    Rollouts(tmp_path / "rollouts", demo_template).write(0)
    intake(c)
    target = c.live_dir / "effective.toml"
    target.write_text(live_config.render(c))
    with catalogue.dataset_lock(c, NAME) as acquired:
        assert acquired
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "levi.live.worker",
                "--config",
                str(target),
                "--dataset",
                NAME,
                "--provider-json",
                json.dumps({"name": "absent", "port": 0}),
            ],
            cwd=PROJECT,
            env=resources.service_env(c),
            capture_output=True,
            text=True,
            check=False,
            timeout=scaled(20),
        )
    assert result.returncode == 15, result.stdout + result.stderr
    assert mirror.load_state(c, NAME).get("current") is None
    ctl = controller.Controller(
        c,
        probes=controller.Probes(
            vram=lambda: None, ports=set, holders=list, policy_vram=lambda _: None
        ),
        log=lambda *args: None,
    )
    try:
        ctl.worker_dataset = NAME
        ctl._reaped(15)
        assert ctl._queue(time.time()) == [NAME]
        assert not ctl.nothing and not ctl.backoff and not ctl.failures
    finally:
        ctl.shutdown()


def wait_prepared(c, started):
    deadline = time.monotonic() + scaled(120)
    while children.identity(started["pid"]) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert children.identity(started["pid"]) is None
    while (
        children.listed(state=c.workspace / "outputs/LEVI/workbench")
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    assert children.listed(state=c.workspace / "outputs/LEVI/workbench") == []


def test_manual_prepare_retries_failed_view_and_ready_is_a_noop(
    tmp_path, demo_template
):
    from levi.conversion.engine import fingerprint

    c = config(tmp_path)
    Rollouts(tmp_path / "rollouts", demo_template).write(0)
    intake(c)
    capture = str(c.captures_dir / NAME)
    failed = {
        "name": NAME,
        "id": f"local/{NAME}",
        "path": capture,
        "kind": "raw",
        "view_status": "failed",
        "view_failed_fingerprint": fingerprint(Path(capture)),
        "view_error": "transient build error",
    }
    jsonio.write(c.workspace / "outputs/LEVI/workbench/datasets.json", {NAME: failed})
    catalogue._record(c, NAME, failed, error=failed["view_error"])
    assert catalogue.names(c) == []
    assert run_catalogue(c)[NAME] == failed  # Automatic passes remain idempotent.
    try:
        started = catalogue.prepare(c, NAME)
        assert (
            started["started"]
            and started["running"]
            and started["reason"] == "preparing"
        )
        wait_prepared(c, started)
        assert catalogue.browse_info(c, NAME)["view_status"] == "ready"
        assert "retry" not in mirror.load_state(c, NAME)["catalogue"]
        assert catalogue.prepare(c, NAME) == {
            "started": False,
            "running": False,
            "reason": "ready",
        }
        assert catalogue.names(c) == []
    finally:
        children.stop_owned(state=c.workspace / "outputs/LEVI/workbench")


def test_manual_prepare_does_not_drop_a_target_into_an_existing_cpu_pass(
    tmp_path, demo_template
):
    c = config(tmp_path)
    Rollouts(tmp_path / "rollouts", demo_template).write(0)
    intake(c)
    before = copy.deepcopy(mirror.load_state(c, NAME))
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        jsonio.write(
            c.live_dir / "catalogue-process.json",
            {"pid": proc.pid, "identity": children.identity(proc.pid)},
        )
        with pytest.raises(ManagementError) as refused:
            catalogue.prepare(c, NAME)
        assert refused.value.status_code == 409
        assert mirror.load_state(c, NAME) == before
    finally:
        proc.terminate()
        proc.wait(timeout=scaled(10))


@pytest.mark.parametrize("owner", ["controller", "external"])
def test_pending_annotation_keeps_admission_but_waits_for_catalogue_process(
    tmp_path, demo_template, monkeypatch, owner
):
    c = config(tmp_path, background=True)
    Rollouts(tmp_path / "rollouts", demo_template).write(0)
    intake(c)
    ctl = controller.Controller(
        c,
        probes=controller.Probes(
            vram=lambda: None, ports=set, holders=list, policy_vram=lambda _: None
        ),
        log=lambda *args: None,
    )
    spawned = []
    monkeypatch.setattr(ctl.vllm, "external", lambda: True)
    monkeypatch.setattr(ctl, "_spawn", lambda name, now=None: spawned.append(name))
    # The CPU process has spawned but deliberately has not taken the dataset
    # lock yet. A queue-only check would race it and start an empty worker.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    state_dir = c.workspace / "outputs/LEVI/workbench"
    children.track(proc, "live-catalogue", state=state_dir)
    jsonio.write(
        c.live_dir / "catalogue-process.json",
        {"pid": proc.pid, "identity": children.identity(proc.pid)},
    )
    if owner == "controller":
        ctl.catalogue = proc
    else:
        monkeypatch.setattr(ctl, "_catalogue_step", lambda now: None)
    t = time.time()
    try:
        assert ctl._queue(t) == [NAME]  # The CPU child has no dataset lock yet.
        ctl.tick(t)
        assert ctl.decision.code == "external" and ctl.state == "active"
        assert spawned == [] and ctl.worker is None
        with catalogue.dataset_lock(c, NAME) as acquired:
            assert acquired
            ctl.tick(t + 1)
            assert ctl.decision.code == "external"  # Still pending, not idle.
            assert spawned == [] and ctl._queue(t + 1) == []
        proc.terminate()
        proc.wait(timeout=scaled(10))
        ctl.tick(t + 2)
        assert spawned == [NAME] and not ctl.nothing and not ctl.failures
    finally:
        ctl.shutdown()
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=scaled(10))
        children.untrack(proc.pid, state=state_dir)


@pytest.mark.parametrize("background", [False, True])
def test_once_waits_for_an_external_cpu_owner_even_when_labelling_cannot_start(
    tmp_path, demo_template, monkeypatch, background
):
    c = config(tmp_path, background=background)
    Rollouts(tmp_path / "rollouts", demo_template).write(0)
    intake(c)
    target = c.live_dir / "effective.toml"
    target.write_text(live_config.render(c))
    release = tmp_path / "release-catalogue"
    script = """import sys, time
from pathlib import Path
while not Path(sys.argv[2]).is_file():
    time.sleep(0.01)
from levi.live import catalogue
raise SystemExit(catalogue.main(['--config', sys.argv[1]]))
"""
    env = resources.service_env(c)
    env["LEVI_CPU_ONLY"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(target), str(release)],
        cwd=PROJECT,
        env=env,
        start_new_session=True,
    )
    state_dir = c.workspace / "outputs/LEVI/workbench"
    children.track(proc, "live-catalogue", state=state_dir)
    jsonio.write(
        c.live_dir / "catalogue-process.json",
        {"pid": proc.pid, "identity": children.identity(proc.pid)},
    )
    ctl = controller.Controller(
        c,
        probes=controller.Probes(
            vram=lambda: None, ports=set, holders=list, policy_vram=lambda _: None
        ),
        log=lambda *args: None,
    )
    monkeypatch.setattr(ctl.vllm, "external", lambda: False)
    ticks = []
    tick = ctl.tick

    def observed_tick():
        ticks.append(1)
        # Deterministic: an early --once exit after the first tick never
        # releases the child. Correct code continues until it has a view.
        if len(ticks) == 2:
            release.write_text("ready")
        return tick()

    monkeypatch.setattr(ctl, "tick", observed_tick)
    try:
        ctl.run(once=True, max_seconds=scaled(120))
        assert len(ticks) >= 2
        assert catalogue.browse_info(c, NAME)["view_status"] == "ready"
        assert ctl.worker is None and not ctl.vllm.mine()
        assert mirror.load_state(c, NAME).get("current") is None
        proc.wait(timeout=scaled(10))
        assert proc.returncode == 0
    finally:
        release.write_text("ready")
        ctl.shutdown()
        children.untrack(proc.pid, grace=15, state=state_dir)
        proc.wait(timeout=scaled(20))
