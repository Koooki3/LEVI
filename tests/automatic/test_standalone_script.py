"""Real dispatch and FR3 core on fake SDK only; no GPU/robot evidence."""

import importlib.util
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_fr3_bridge import (  # noqa: F401 - imported pytest safety fixture
    no_network,
    rig,
)

from levi.automatic import api, cli, control, fr3_run, launch


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


def tracked(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setenv("LEVI_AERI_HOME", str(tmp_path / "aeri-home"))
    world = rig(tmp_path, monkeypatch, **kwargs)
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
    world = tracked(tmp_path, monkeypatch, scenes=("unknown",))
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
