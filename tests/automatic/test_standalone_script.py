"""Real dispatch and FR3 core on fake SDK only; no GPU/robot evidence."""

import importlib.util
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_api_common import client, home, roots  # noqa: F401 - HTTP fixtures
from test_fr3_bridge import (  # noqa: F401 - imported pytest safety fixture
    no_network,
    rig,
    without_a_terminal,
)
from test_launch import (  # noqa: F401 - isolated fake launcher fixture
    fake_systemd,
    write_job,
)

from levi import children
from levi.automatic import api, cli, control, fr3_run, launch
from levi.automatic.journal import Journal


def script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "aeri.py"
    spec = importlib.util.spec_from_file_location("aeri_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_thin_script_dispatches_to_original_entry(monkeypatch):
    calls = []
    monkeypatch.setattr(fr3_run, "main", lambda args: calls.append(args) or 7)
    assert script().main(["check", "--config", "job", "--binding", "binding"]) == 7
    assert calls == [["--config", "job", "--binding", "binding", "--check"]]
    monkeypatch.setattr(cli, "main", lambda args: calls.append(args) or 0)
    assert script().main(["runs", "--json"]) == 0
    assert calls[-1] == ["runs", "--json"]


def tracked(tmp_path, monkeypatch, *, plain_wait=False, **kwargs):
    """``plain_wait``: the old no-console WAIT_HUMAN path (the tracked select
    wait), unreachable on a real run since L2-UI2-PROVIDER-RELEASE and kept
    under test while its code exists (``without_a_terminal``)."""
    monkeypatch.setenv("LEVI_AERI_HOME", str(tmp_path / "aeri-home"))
    world = rig(tmp_path, monkeypatch, **kwargs)
    if plain_wait:
        without_a_terminal(monkeypatch)
    world.job["standalone_inputs"] = {
        "job": {"sha256": "a" * 64},
        "binding_sha256": "b" * 64,
    }
    return world


def test_actual_sdk_mock_execution_registers_shared_cli_api_source(
    tmp_path, monkeypatch
):
    world = tracked(tmp_path, monkeypatch)
    assert (
        fr3_run.execute(
            world.job, world.binding, load_modules=lambda binding: world.modules
        )
        == "COMPLETED"
    )
    entry, directory = launch.registered_run("real-test")
    assert (
        entry["origin"] == "operator_terminal" and entry["execution_mode"] == "assisted"
    )
    assert entry["runner"]["state"] == "exited" and not launch.runner_alive(
        entry["runner"]
    )
    assert (
        api._locate("real-test").run_dir
        == cli._launched(SimpleNamespace(run="real-test", run_dir=None))
        == directory
    )
    summary = api._summary(api._locate("real-test"))
    assert summary["origin"] == "operator_terminal" and summary["state"] == "COMPLETED"
    assert launch.runs()[0]["run_id"] == "real-test"
    # A real standalone has no dry-run launch request and cannot be attached/replayed.
    with pytest.raises(launch.LaunchRefused):
        launch.attach(directory, backend="inprocess")


@pytest.mark.parametrize(
    "tamper", ["index_path", "record_path", "header", "plan", "fake_record"]
)
def test_standalone_registration_does_not_trust_fabricated_or_changed_files(
    tmp_path, monkeypatch, tamper
):
    world = tracked(tmp_path, monkeypatch)
    fr3_run.execute(
        world.job, world.binding, load_modules=lambda binding: world.modules
    )
    entry, directory = launch.registered_run("real-test")
    if tamper == "index_path":
        entry["run_dir"] = str(tmp_path / "real-test")
        launch.write_record(launch.index_path("real-test"), entry)
    elif tamper in ("record_path", "fake_record"):
        record = launch.read_record(directory / launch.LAUNCH_RECORD)
        record["rollout_root"] = str(tmp_path)
        if tamper == "fake_record":
            record.pop("registration_mac")
        launch.write_record(directory / launch.LAUNCH_RECORD, record)
    elif tamper == "plan":
        (directory / "plan.json").write_text("{}")
    else:
        (directory / "state_journal.jsonl").write_text("{}\n")
    with pytest.raises(HTTPException) as exc:
        api._locate("real-test")
    assert exc.value.status_code == 404
    assert launch.runs() == []


def test_standalone_keeps_driving_without_a_page_and_uses_original_command_pump(
    tmp_path, monkeypatch
):
    world = tracked(tmp_path, monkeypatch, plain_wait=True, scenes=("unknown",))
    monkeypatch.setattr(fr3_run.sys.stdin, "isatty", lambda: True)

    def no_terminal_input(*args):
        time.sleep(0.01)
        return [], [], []

    monkeypatch.setattr(fr3_run.select, "select", no_terminal_input)
    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            fr3_run.execute(
                world.job, world.binding, load_modules=lambda binding: world.modules
            )
        )
    )
    thread.start()
    directory = launch.run_dir_of(Path(world.job["rollout_root"]), "real-test")
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                run = api._locate("real-test")
                if run.state == "WAIT_HUMAN" and run.runner()["alive"]:
                    break
            except HTTPException:
                pass
            time.sleep(0.01)
        else:
            pytest.fail("standalone never reached registered waiting state")
        # Stale sequence is refused by the existing state machine, no extra channel.
        bad = control.command(
            "resume",
            "resume-old",
            "operator",
            expected_seq=0,
            environment_handled=True,
            health_rechecked=True,
        )
        control.write_command(directory, bad)
        refused = control.wait_result(directory, "resume-old", 2)
        assert not refused["ok"]
        value = control.command("stop", "stop-once", "operator")
        assert control.write_command(directory, value) == "queued"
        assert control.write_command(directory, value) == "already_queued"
        stopped = control.wait_result(directory, "stop-once", 2)
        assert stopped["ok"]
        thread.join(3)
        assert not thread.is_alive() and result
        assert api._locate("real-test").state == "COMPLETED"
    finally:
        if thread.is_alive():
            control.write_command(
                directory, control.command("stop", "cleanup-stop", "operator")
            )
            thread.join(3)


@pytest.mark.parametrize("signum", ["SIGTERM", "SIGINT"])
def test_signal_in_tracked_wait_human_never_blocks_in_input(
    tmp_path, monkeypatch, signum
):
    import signal

    world = tracked(tmp_path, monkeypatch, plain_wait=True, scenes=("unknown",))
    monkeypatch.setattr(fr3_run.sys.stdin, "isatty", lambda: True)
    sent = []

    def deliver_signal_instead_of_terminal_input(*args):
        if not sent:
            sent.append(signum)
            number = getattr(signal, signum)
            signal.getsignal(number)(number, None)
        return [], [], []

    def forbidden_input(*args):
        pytest.fail("a signalled WAIT_HUMAN run fell into the blocking input()")

    monkeypatch.setattr(
        fr3_run.select, "select", deliver_signal_instead_of_terminal_input
    )
    monkeypatch.setattr("builtins.input", forbidden_input)
    fr3_run.execute(
        world.job, world.binding, load_modules=lambda binding: world.modules
    )
    assert sent == [signum]
    assert world.calls[-1][0] == "closed"


def test_fabricated_unknown_directory_is_not_a_registered_run(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVI_AERI_HOME", str(tmp_path / "home"))
    launch.ensure_home()
    index = launch.home() / launch.RUNS
    index.mkdir()
    directory = tmp_path / "unknown"
    directory.mkdir()
    launch.write_record(directory / launch.LAUNCH_RECORD, {})
    launch.write_record(
        launch.index_path("unknown"), {"run_id": "unknown", "run_dir": str(directory)}
    )
    with pytest.raises(HTTPException) as exc:
        api._locate("unknown")
    assert exc.value.status_code == 404


def test_runner_identity_is_bound_to_registered_writer(tmp_path, monkeypatch):
    world = tracked(tmp_path, monkeypatch)
    fr3_run.execute(
        world.job, world.binding, load_modules=lambda binding: world.modules
    )
    entry, directory = launch.registered_run("real-test")
    runner = dict(entry["runner"], state="running")
    assert launch.registered_runner_alive(entry, runner)
    runner["identity"] = {"start_ticks": "wrong", "boot": "wrong"}
    launch.write_record(directory / launch.RUNNER_RECORD, runner)
    assert not api._locate("real-test").runner()["alive"]
    assert not launch.registered_runner_alive(entry, runner)


def test_runner_record_must_match_registered_plan_even_if_process_is_alive(
    tmp_path, monkeypatch
):
    world = tracked(tmp_path, monkeypatch)
    fr3_run.execute(
        world.job, world.binding, load_modules=lambda binding: world.modules
    )
    entry, _ = launch.registered_run("real-test")
    # A live process (this one) with a self-consistent identity, but a plan the
    # registration never named, must not count as the registered writer.
    me = {"pid": os.getpid(), "identity": children.identity(os.getpid())}
    runner = dict(entry["runner"], state="running", **me)
    assert launch.runner_alive(runner)
    other = dict(entry, registration=dict(entry["registration"], **me))
    assert launch.registered_runner_alive(
        other, dict(runner, plan_sha256=other["registration"]["plan_sha256"])
    )
    assert not launch.registered_runner_alive(other, dict(runner, plan_sha256="0" * 64))


def test_faulted_standalone_stays_locked_and_cannot_be_replayed(tmp_path, monkeypatch):
    world = tracked(tmp_path, monkeypatch, faults=("no_ack",))
    assert (
        fr3_run.execute(
            world.job, world.binding, load_modules=lambda binding: world.modules
        )
        == "FAULT_LOCKED"
    )
    run = api._locate("real-test")
    assert run.state == "FAULT_LOCKED" and not run.runner()["alive"]
    with pytest.raises(launch.LaunchRefused):
        launch.attach(run.run_dir, backend="inprocess")


def test_real_standalone_cannot_be_downgraded_to_an_unsigned_dry_launch(
    tmp_path, monkeypatch
):
    world = tracked(tmp_path, monkeypatch)
    fr3_run.execute(
        world.job, world.binding, load_modules=lambda binding: world.modules
    )
    entry, directory = launch.registered_run("real-test")
    request = launch.LaunchRequest(
        job_path=str(tmp_path / "job.yaml"),
        execution_mode=launch.DRY_RUN,
        backend="foreground",
        entry="cli",
        principal={"kind": "operator", "id": "test"},
        keep_dir=world.job["rollout_root"],
    )
    fake = {
        "schema": launch.LAUNCH_SCHEMA,
        "run_id": "real-test",
        "plan_sha256": entry["plan_sha256"],
        "request": request.record(),
    }
    launch.write_record(directory / launch.LAUNCH_RECORD, fake)
    entry.update(backend="foreground", execution_mode="dry_run")
    entry.pop("registration", None)
    launch.write_record(launch.index_path("real-test"), entry)
    with pytest.raises(HTTPException) as exc:
        api._locate("real-test")
    assert exc.value.status_code == 404


@pytest.mark.parametrize("tail", [b"{}\n", b"{partial"])
def test_active_writer_rejects_disk_corruption_before_resume(
    tmp_path, monkeypatch, tail
):
    world = tracked(tmp_path, monkeypatch, plain_wait=True, scenes=("unknown",))
    monkeypatch.setattr(fr3_run.sys.stdin, "isatty", lambda: True)

    def no_input(*args):
        time.sleep(0.01)
        return [], [], []

    monkeypatch.setattr(fr3_run.select, "select", no_input)
    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            fr3_run.execute(
                world.job, world.binding, load_modules=lambda binding: world.modules
            )
        )
    )
    thread.start()
    directory = launch.run_dir_of(Path(world.job["rollout_root"]), "real-test")
    try:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                run = api._locate("real-test")
                if run.state == "WAIT_HUMAN" and run.runner()["alive"]:
                    break
            except HTTPException:
                pass
            time.sleep(0.01)
        else:
            pytest.fail("writer did not wait")
        journal = directory / "state_journal.jsonl"
        with journal.open("ab") as stream:
            stream.write(tail)
        read = api._locate("real-test")
        assert read.state == "FAULT_LOCKED"
        assert api.snapshot_of(read)["challenge"] is None
        monkeypatch.setattr(api, "_person", lambda request: None)
        body = api.ResumeBody(
            command_id="api-resume",
            expected_seq=read.seq,
            environment_handled=True,
            health_rechecked=True,
            challenge="old-challenge",
        )
        with pytest.raises(HTTPException) as refusal:
            api.resume_run("real-test", body, None)
        assert refusal.value.status_code == 409
        assert refusal.value.detail["code"] == "journal_corrupt"
        assert (
            cli.cmd_resume(
                SimpleNamespace(
                    run="real-test",
                    run_dir=None,
                    principal="operator",
                    command_id=None,
                    json=True,
                )
            )
            == cli.EXIT_REFUSED
        )
        command = control.command(
            "resume",
            "corrupt-resume",
            "operator",
            expected_seq=read.seq,
            environment_handled=True,
            health_rechecked=True,
        )
        with pytest.raises(control.CommandError, match="journal_corrupt"):
            control.write_command(directory, command)
        # Model a valid command queued earlier: the real pump must recheck at
        # execution time, independently of the writer/API precheck.
        paths = control.ensure_folders(directory)
        control._link_whole(
            paths["inbox"], "corrupt-resume.json", (json.dumps(command) + "\n").encode()
        )
        refused = control.wait_result(directory, "corrupt-resume", 1)
        assert refused is not None and not refused["ok"]
        assert (
            refused["code"] == "journal_corrupt" and refused["state"] == "FAULT_LOCKED"
        )
        thread.join(1)
        assert not thread.is_alive() and result == ["FAULT_LOCKED"]
        assert journal.read_bytes().endswith(tail)
        # Stop remains queueable despite damaged diagnostic history.
        assert (
            control.write_command(
                directory, control.command("stop", "safe-stop", "operator")
            )
            == "queued"
        )
    finally:
        if thread.is_alive():
            control.write_command(
                directory, control.command("stop", "cleanup-stop", "operator")
            )
            thread.join(1)


def test_fabricated_legacy_dry_registration_without_header_and_plan_is_refused(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LEVI_AERI_HOME", str(tmp_path / "home"))
    launch.ensure_home()
    (launch.home() / launch.RUNS).mkdir()
    run_id = "fake-dry"
    request = launch.LaunchRequest(
        job_path=str(tmp_path / "never-approved.yaml"),
        execution_mode="dry_run",
        backend="foreground",
        entry="cli",
        principal={"kind": "operator", "id": "fake"},
        keep_dir=str(tmp_path / "rollouts"),
    )
    directory = launch.run_dir_of(launch.dry_folder(run_id, request.keep_dir), run_id)
    directory.mkdir(parents=True)
    launch.write_record(
        directory / launch.LAUNCH_RECORD,
        {
            "schema": launch.LAUNCH_SCHEMA,
            "run_id": run_id,
            "request": request.record(),
            "plan_sha256": "a" * 64,
        },
    )
    launch.write_record(
        launch.index_path(run_id),
        {
            "schema": launch.INDEX_SCHEMA,
            "run_id": run_id,
            "run_dir": str(directory),
            "execution_mode": "dry_run",
            "backend": "foreground",
            "plan_sha256": "a" * 64,
        },
    )
    with pytest.raises(HTTPException):
        api._locate(run_id)
    with pytest.raises(cli.JobError):
        cli._launched(SimpleNamespace(run=run_id, run_dir=None))
    assert not (launch.home() / launch.KEY_FILE).exists()


@pytest.mark.usefixtures("fake_systemd")
@pytest.mark.parametrize("change", ["inputs", "run_id", "mode"])
def test_only_authentic_dry_starting_record_is_visible(tmp_path, monkeypatch, change):
    monkeypatch.setenv("LEVI_AERI_HOME", str(tmp_path / "home"))
    job = write_job(tmp_path)
    request = launch.LaunchRequest(
        job_path=str(job),
        execution_mode="dry_run",
        backend="systemd",
        entry="cli",
        principal={"kind": "operator", "id": "fake"},
    )
    plan = launch.plan(request)
    handle = launch.launch(
        request, plan_sha256=plan.plan_sha256, launch_token=plan.launch_token
    )
    directory = Path(handle.run_dir)
    assert not (directory / "state_journal.jsonl").exists()
    assert api._locate(handle.run_id).state == "INIT"
    record = launch.read_record(directory / launch.LAUNCH_RECORD)
    assert record["inputs"]["job"] == launch.job_fact(job)
    if change == "inputs":
        record["inputs"]["job"]["sha256"] = "f" * 64
    elif change == "run_id":
        record["run_id"] = "copied-id"
    else:
        record["request"]["execution_mode"] = "assisted"
    launch.write_record(directory / launch.LAUNCH_RECORD, record)
    with pytest.raises(HTTPException):
        api._locate(handle.run_id)


@pytest.mark.usefixtures("fake_systemd")
@pytest.mark.parametrize(
    "phase",
    ["plan_only", "header_during_read", "wrong_plan", "torn_header", "corrupt_header"],
)
def test_authenticated_dry_start_is_visible_across_plan_and_header_publication(
    tmp_path, monkeypatch, phase
):
    """The runner publishes plan before header; readers may straddle both writes."""
    from levi.automatic import journal

    monkeypatch.setenv("LEVI_AERI_HOME", str(tmp_path / "home"))
    job = write_job(tmp_path)
    request = launch.LaunchRequest(
        job_path=str(job),
        execution_mode="dry_run",
        backend="systemd",
        entry="cli",
        principal={"kind": "operator", "id": "test"},
    )
    planned = launch.plan(request)
    handle = launch.launch(
        request, plan_sha256=planned.plan_sha256, launch_token=planned.launch_token
    )
    directory = Path(handle.run_dir)
    record = launch.read_record(directory / launch.LAUNCH_RECORD)
    frozen = record["plan"]
    if phase == "header_during_read":
        original = journal.read_plan
        published = False

        def publish_after_read(where):
            nonlocal published
            kept = original(where)
            if not published:
                published = True
                Journal.create(
                    directory,
                    run_id=handle.run_id,
                    plan_sha256=planned.plan_sha256,
                    levi_commit=None,
                    authority={
                        "principal_kind": "orchestrator",
                        "principal_id": "test",
                        "session_id": "test",
                        "process": journal.process_identity(),
                        "command_id": None,
                    },
                    plan=frozen,
                ).close()
            return kept

        monkeypatch.setattr(journal, "read_plan", publish_after_read)
    else:
        kept = dict(frozen)
        if phase == "wrong_plan":
            kept["mode"] = "assisted"
        journal.keep_plan(directory, kept, journal.plan_digest(kept))
        if phase == "torn_header":
            (directory / journal.JOURNAL).write_bytes(b'{"partial":')
        elif phase == "corrupt_header":
            (directory / journal.JOURNAL).write_bytes(b"{}\n")
    if phase in ("wrong_plan", "torn_header", "corrupt_header"):
        with pytest.raises(launch.LaunchRefused):
            launch.registered_run(handle.run_id)
    else:
        entry, found = launch.registered_run(handle.run_id)
        assert entry["run_id"] == handle.run_id and found == directory
        expected = "PREFLIGHT" if phase == "header_during_read" else "INIT"
        assert api._locate(handle.run_id).state == expected


def test_http_resume_of_a_standalone_real_run_is_refused_and_writes_nothing(
    client,  # noqa: F811 - fixture
    tmp_path,
    monkeypatch,
):
    """User decision 2026-10-13: a registered standalone real run is resumed
    only at its terminal (robot.health() rechecked by the driver), never by
    the page's self-attested checkboxes."""
    world = tracked(tmp_path, monkeypatch, plain_wait=True, scenes=("unknown",))
    monkeypatch.setattr(fr3_run.sys.stdin, "isatty", lambda: True)

    def no_terminal_input(*args):
        time.sleep(0.01)
        return [], [], []

    monkeypatch.setattr(fr3_run.select, "select", no_terminal_input)
    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            fr3_run.execute(
                world.job, world.binding, load_modules=lambda binding: world.modules
            )
        )
    )
    thread.start()
    directory = launch.run_dir_of(Path(world.job["rollout_root"]), "real-test")
    base = "/api/levi/automatic/runs/real-test"
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                run = api._locate("real-test")
                if run.state == "WAIT_HUMAN" and run.runner()["alive"]:
                    break
            except HTTPException:
                pass
            time.sleep(0.01)
        else:
            pytest.fail("standalone never reached registered waiting state")
        snap = client.get(base).json()
        assert snap["state"] == "WAIT_HUMAN" and snap["resume_terminal_only"] is True
        # Decision UI-2: nor is a scene question answered from the page.
        assert snap["scene_answer_terminal_only"] is True
        scene = client.post(
            f"{base}/scene-answer",
            json={
                "request_id": "scene-any",
                "nonce": "n",
                "frames_sha256": "ab" * 32,
                "predicates": {"object_at_source": True},
            },
        )
        assert scene.status_code == 409, scene.text
        assert scene.json()["detail"]["code"] == "scene_answer_terminal_only"
        lines_before = len(Journal.read(directory).events)
        commands = directory / "control"
        before = (
            sorted(p.name for p in commands.rglob("*")) if commands.exists() else []
        )
        body = {
            "command_id": "resume-web",
            "expected_seq": snap["seq"],
            "environment_handled": True,
            "health_rechecked": True,
            "challenge": snap["challenge"],
        }
        refused = client.post(f"{base}/resume", json=body)
        assert refused.status_code == 403, refused.text
        detail = refused.json()["detail"]
        assert detail["code"] == "terminal_resume_required"
        assert "scripts/aeri.py" in detail["message"]
        assert "levi automatic resume" not in detail["message"]
        # Nothing was consumed or written: same challenge still held, no
        # command file, journal unchanged, the run still waits.
        assert api._CHALLENGES.by_run["real-test"][snap["challenge"]][0] == snap["seq"]
        after = sorted(p.name for p in commands.rglob("*")) if commands.exists() else []
        assert after == before
        assert len(Journal.read(directory).events) == lines_before
        assert api._locate("real-test").state == "WAIT_HUMAN"
        # Stopping from the page stays possible.
        stop = client.post(
            f"{base}/stop", json={"command_id": "stop-web", "confirm": "stop"}
        )
        assert stop.status_code == 200 and stop.json()["result"] == "applied"
        thread.join(3)
        assert not thread.is_alive()
    finally:
        if thread.is_alive():
            control.write_command(
                directory, control.command("stop", "cleanup-stop", "operator")
            )
            thread.join(3)
