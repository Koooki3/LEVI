"""The launch core (T-CL-07, levi/automatic/launch.py): the plan and its
digest, the launch token (TOCTOU), the checks (execution mode, run id,
robot lock, other evaluation clients, scene provider, contract), the
backends (a fake ``systemd-run``: no unit is ever started) and the runner's
own refusals. ``LEVI_AERI_HOME`` always points into ``tmp_path``."""

import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest
from test_aeri_cli import CONTRACT, job_text

from levi.automatic import cli, launch, runner
from levi.automatic.journal import Journal, read_plan

HERE = Path(__file__).resolve().parent

FAKE_SYSTEMD = """#!{python}
import json, os, subprocess, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_SYSTEMD_LOG"], "a") as handle:
    handle.write(json.dumps(argv) + "\\n")
if os.environ.get("FAKE_SYSTEMD_FAIL"):
    print("Failed to start transient service unit", file=sys.stderr)
    sys.exit(1)
if os.environ.get("FAKE_SYSTEMD_EXEC"):
    env = dict(os.environ)
    for item in argv:
        if item.startswith("--setenv="):
            name, _, value = item[len("--setenv="):].partition("=")
            env[name] = value
    command = argv[argv.index("-m") - 1:]
    out = open(os.environ["FAKE_SYSTEMD_LOG"] + ".unit.log", "ab")
    subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=out,
                     stderr=out, start_new_session=True)
"""


@pytest.fixture(autouse=True)
def aeri_home(tmp_path, monkeypatch):
    """Never the real ~/.levi-aeri."""
    folder = tmp_path / "aeri-home"
    monkeypatch.setenv(launch.HOME_ENV, str(folder))
    monkeypatch.delenv(launch.JOB_ROOTS_ENV, raising=False)
    return folder


@pytest.fixture
def no_tracking(monkeypatch):
    """The product's processes.json never learns of a runner."""
    import levi.children

    def refuse(*args, **kwargs):
        raise AssertionError("a runner must not be tracked by the product")

    monkeypatch.setattr(levi.children, "track", refuse)


def write_job(tmp_path, name="r-cli", strategy="single_reset_policy", **over):
    folder = tmp_path / f"job-{name}"
    folder.mkdir()
    root = tmp_path / "rollouts"
    root.mkdir(exist_ok=True)
    (folder / "initial-state.yaml").write_text(over.pop("contract", CONTRACT))
    path = folder / "automatic-eval.yaml"
    path.write_text(
        job_text(
            root,
            name=name,
            strategy=strategy,
            forward_folder=f"stack__{name}",
            reset_folder=f"reset_stack__{name}",
            **over,
        )
    )
    return path


@pytest.fixture
def job(tmp_path):
    return write_job(tmp_path)


@pytest.fixture
def fake_systemd(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    program = bin_dir / "systemd-run"
    program.write_text(FAKE_SYSTEMD.format(python=sys.executable))
    program.chmod(0o755)
    log = tmp_path / "systemd-run.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SYSTEMD_LOG", str(log))
    return log


def calls(log: Path) -> list:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def request(job, mode="dry_run", **kw):
    return launch.LaunchRequest(job_path=str(job), execution_mode=mode, **kw)


def launched(job, mode="dry_run", **kw):
    found = launch.plan(request(job, mode, **kw))
    return found, launch.launch(
        request(job, mode, **kw),
        plan_sha256=found.plan_sha256,
        launch_token=found.launch_token,
    )


# --- the plan --------------------------------------------------------------------------------------


def test_the_plan_digest_is_the_job_loaders_without_overrides(job):
    found = launch.plan(launch.LaunchRequest(job_path=str(job)))
    assert found.plan_sha256 == cli.load_job(job)["plan_sha256"]
    # The job's own mode (shadow) has no adapter in this version.
    assert found.execution_mode == "shadow" and not found.launchable
    assert "E_NO_ROBOT_ADAPTER" in found.refusals


def test_the_digest_ignores_who_asks_and_how_it_is_hosted(job):
    digests = {
        launch.plan(
            request(
                job,
                entry=entry,
                backend=backend,
                principal={"kind": "operator", "id": who},
            )
        ).plan_sha256
        for entry in ("cli", "api")
        for backend in launch.BACKENDS
        for who in ("operator", "op-7")
    }
    assert len(digests) == 1


def test_mode_and_overrides_enter_the_digest(job):
    base = launch.plan(request(job)).plan_sha256
    assert launch.plan(request(job, "assisted")).plan_sha256 != base
    more = launch.plan(request(job, overrides={"episodes": 5}))
    assert more.plan_sha256 != base and more.episodes == 5
    assert more.plan["run"]["episodes"] == 5
    scenes = launch.plan(request(job, overrides={"scenes": ["reset_required"]}))
    assert scenes.plan_sha256 != base
    assert scenes.plan["dry_run"] == {"scenes": ["reset_required"]}


@pytest.mark.parametrize(
    "overrides",
    [
        {"episodes": -1},
        {"episodes": True},
        {"episodes": "3"},
        {"scenes": ["maybe"]},
        {"robot": "fr3"},
    ],
)
def test_overrides_outside_the_allowlist_are_refused(job, overrides):
    found = launch.plan(request(job, overrides=overrides))
    assert not found.launchable and found.refusals == ["E_OVERRIDE"]


def test_scenes_script_the_fakes_of_a_dry_run_only(job):
    found = launch.plan(request(job, "shadow", overrides={"scenes": ["ready"]}))
    assert "E_OVERRIDE" in found.refusals


def test_the_plan_writes_nothing_but_the_core_key(job, aeri_home, tmp_path):
    def outside():
        return sorted(
            (str(p), p.stat().st_mtime_ns)
            for p in tmp_path.rglob("*")
            if p != aeri_home and aeri_home not in p.parents
        )

    before = outside()
    for mode in launch.MODES:
        launch.plan(request(job, mode))
    assert outside() == before
    assert sorted(p.name for p in aeri_home.iterdir()) == ["core.key"]
    assert stat.S_IMODE(aeri_home.stat().st_mode) == 0o700
    assert stat.S_IMODE((aeri_home / "core.key").stat().st_mode) == 0o600


def test_ignored_reset_keys_stay_out_of_the_human_assisted_digest(tmp_path):
    a = write_job(tmp_path, name="h1", strategy="human_assisted")
    first = launch.plan(request(a)).plan_sha256
    a.write_text(a.read_text().replace("max_attempts: 1", "max_attempts: 4"))
    assert launch.plan(request(a)).plan_sha256 == first


@pytest.mark.parametrize("mode", ["shadow", "assisted", "autonomous"])
def test_real_modes_are_never_launchable(job, mode, fake_systemd):
    found = launch.plan(request(job, mode))
    assert not found.launchable and found.motion
    assert "E_NO_ROBOT_ADAPTER" in found.refusals
    assert found.needs_arming == (mode != "shadow")
    # Even with the plan's own valid token, launch() refuses again.
    with pytest.raises(launch.LaunchRefused) as caught:
        launch.launch(
            request(job, mode),
            plan_sha256=found.plan_sha256,
            launch_token=found.launch_token,
        )
    assert caught.value.code == "E_NO_ROBOT_ADAPTER" and caught.value.status == 501
    assert calls(fake_systemd) == []


def test_a_dry_run_plan_is_launchable_and_says_what_runs(job, aeri_home):
    found = launch.plan(request(job))
    assert found.launchable and found.refusals == [] and not found.motion
    assert found.adapters["robot"] == "fake" and found.roles == ["forward", "reset"]
    assert found.isc["id"] == "stack-plates-initial" and found.isc["status"] == "draft"
    assert any(c["code"] == "W_CONTRACT_DRAFT" for c in found.checks)
    assert found.run_dir == str(
        aeri_home / "dry-runs" / "r-cli" / ".aeri" / "runs" / "r-cli"
    )
    assert json.loads(json.dumps(found.public()))["plan_sha256"] == found.plan_sha256


def test_human_assisted_needs_a_scene_provider_for_a_real_run(tmp_path):
    job = write_job(tmp_path, name="h2", strategy="human_assisted")
    dry = launch.plan(request(job))
    assert dry.launchable and dry.roles == ["forward"]
    assert dry.adapters["policy_reset"] is None
    real = launch.plan(request(job, "assisted"))
    assert "E_SCENE_PROVIDER_MISSING" in real.refusals


def test_operator_attested_belongs_to_human_assisted(tmp_path):
    job = write_job(tmp_path, name="h3", extra="", strategy="single_reset_policy")
    job.write_text(
        job.read_text().replace(
            "on_unknown: reset", "on_unknown: reset\n  scene_check: operator_attested"
        )
    )
    found = launch.plan(request(job))
    assert found.refusals == ["E_JOB_INVALID"] and found.plan_sha256 is None


def test_operator_attested_dry_run_uses_the_scripted_person(tmp_path):
    job = write_job(tmp_path, name="h4", strategy="human_assisted")
    job.write_text(
        job.read_text().replace(
            "on_unknown: reset", "on_unknown: reset\n  scene_check: operator_attested"
        )
    )
    found = launch.plan(request(job))
    assert found.launchable and found.scene_check == "operator_attested"
    assert found.adapters["scene"] == "human-scripted"


def test_a_real_run_needs_an_initial_state_contract(tmp_path):
    job = write_job(tmp_path, name="nc")
    job.write_text(
        job.read_text().replace("  initial_state_spec: initial-state.yaml\n", "")
    )
    assert "E_NO_CONTRACT" in launch.plan(request(job, "shadow")).refusals
    assert launch.plan(request(job)).launchable  # a dry run warns only


def test_an_invalid_job_gives_a_plan_without_digest(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("schema_version: nope\n")
    found = launch.plan(request(path))
    assert found.plan_sha256 is None and found.refusals == ["E_JOB_INVALID"]
    assert found.launch_token is None
    missing = launch.plan(request(tmp_path / "none.yaml"))
    assert missing.refusals == ["E_JOB_INVALID"]


def test_a_relative_job_path_is_refused(job):
    found = launch.plan(launch.LaunchRequest(job_path="automatic-eval.yaml"))
    assert found.refusals == ["E_REQUEST"]


def test_the_api_picks_job_files_under_the_job_roots(job, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv(launch.JOB_ROOTS_ENV, str(other))
    assert "E_JOB_OUTSIDE_ROOTS" in launch.plan(request(job, entry="api")).refusals
    assert launch.plan(request(job, entry="cli")).launchable
    monkeypatch.setenv(launch.JOB_ROOTS_ENV, f"{other}{os.pathsep}{job.parent}")
    assert launch.plan(request(job, entry="api")).launchable
    listed = launch.jobs()
    assert [j["name"] for j in listed] == ["automatic-eval.yaml", "initial-state.yaml"]


# --- the token ---------------------------------------------------------------------------------------


def test_an_edited_job_file_is_refused_with_the_old_token(job, fake_systemd):
    found = launch.plan(request(job))
    job.write_text(job.read_text().replace("episodes: 2", "episodes: 3"))
    with pytest.raises(launch.LaunchRefused) as caught:
        launch.launch(
            request(job), plan_sha256=found.plan_sha256, launch_token=found.launch_token
        )
    assert caught.value.code == "E_PLAN_CHANGED" and caught.value.status == 412
    assert calls(fake_systemd) == []


def test_a_touched_job_file_invalidates_the_token(job, fake_systemd):
    found = launch.plan(request(job))
    later = time.time_ns() + 5_000_000_000
    os.utime(job, ns=(later, later))
    # Same bytes, same digest: the file is not the one that was read.
    assert launch.plan(request(job)).plan_sha256 == found.plan_sha256
    with pytest.raises(launch.LaunchRefused) as caught:
        launch.launch(
            request(job), plan_sha256=found.plan_sha256, launch_token=found.launch_token
        )
    assert caught.value.code == "E_PLAN_CHANGED"


def test_a_contract_edit_changes_the_plan(job, fake_systemd):
    found = launch.plan(request(job))
    contract = job.parent / "initial-state.yaml"
    contract.write_text(
        contract.read_text().replace("min_evidence_refs: 1", "min_evidence_refs: 2")
    )
    with pytest.raises(launch.LaunchRefused) as caught:
        launch.launch(
            request(job), plan_sha256=found.plan_sha256, launch_token=found.launch_token
        )
    assert caught.value.code == "E_PLAN_CHANGED"


def test_an_expired_or_forged_token_is_refused(job, fake_systemd):
    now = time.time_ns()
    found = launch.plan(request(job), now_ns=now)
    with pytest.raises(launch.LaunchRefused) as caught:
        launch.launch(
            request(job),
            plan_sha256=found.plan_sha256,
            launch_token=found.launch_token,
            now_ns=now + launch.TOKEN_TTL_NS + 1,
        )
    assert caught.value.code == "E_TOKEN_EXPIRED"
    version, expires, mac = found.launch_token.split(".")
    for token in (
        "garbage",
        None,
        f"{version}.{int(expires) + 10**12}.{mac}",  # a later expiry
        f"{version}.{expires}.{'0' * 64}",
    ):
        with pytest.raises(launch.LaunchRefused) as caught:
            launch.launch(
                request(job), plan_sha256=found.plan_sha256, launch_token=token
            )
        assert caught.value.code in ("E_TOKEN_INVALID", "E_PLAN_CHANGED")
    with pytest.raises(launch.LaunchRefused) as caught:
        launch.launch(
            request(job), plan_sha256="0" * 64, launch_token=found.launch_token
        )
    assert caught.value.code == "E_PLAN_CHANGED"
    assert calls(fake_systemd) == []


def test_a_token_of_another_home_is_refused(job, tmp_path, monkeypatch, fake_systemd):
    found = launch.plan(request(job))
    monkeypatch.setenv(launch.HOME_ENV, str(tmp_path / "other-home"))
    with pytest.raises(launch.LaunchRefused) as caught:
        launch.launch(
            request(job), plan_sha256=found.plan_sha256, launch_token=found.launch_token
        )
    assert caught.value.code == "E_PLAN_CHANGED"


def test_a_core_key_others_can_read_is_refused(job, aeri_home):
    launch.plan(request(job))
    (aeri_home / "core.key").chmod(0o644)
    found = launch.plan(request(job))
    assert "E_KEY" in found.refusals and found.launch_token is None


# --- robot mutual exclusion --------------------------------------------------------------------------


def test_a_held_robot_lock_refuses_a_real_plan(job):
    held = launch.RobotLock("fr3", {"run_id": "other"})
    try:
        found = launch.plan(request(job, "assisted"))
        assert "E_ROBOT_BUSY" in found.refusals
        with pytest.raises(launch.LaunchRefused) as caught:
            launch.RobotLock("fr3", {"run_id": "second"})
        assert caught.value.code == "E_ROBOT_BUSY"
    finally:
        held.close()
    found = launch.plan(request(job, "assisted"))
    assert "E_ROBOT_BUSY" not in found.refusals
    # The fake robot of a dry run is the run's own: no shared lock.
    assert launch.plan(request(job)).launchable


def _session(root: Path, name: str, **values):
    folder = root / ".eval_sessions"
    folder.mkdir(exist_ok=True)
    body = {
        "schema": "levi.eval.session.v1",
        "group": "g",
        "task_folder": name,
        "state": "running",
        "updated_at": time.time(),
        "pid": os.getpid(),
        **values,
    }
    (folder / f"g__{name}.json").write_text(json.dumps(body))


def test_a_live_evaluation_client_refuses_a_real_plan(job, tmp_path):
    root = tmp_path / "rollouts"
    _session(root, "aeri", aeri={"run_id": "x", "state": "PREFLIGHT"})
    _session(root, "done", state="stopped")
    _session(root, "old", updated_at=time.time() - 7200, pid=None)
    assert "E_ROBOT_BUSY_CLIENT" not in launch.plan(request(job, "assisted")).refusals
    _session(root, "client")
    found = launch.plan(request(job, "assisted"))
    assert "E_ROBOT_BUSY_CLIENT" in found.refusals
    assert [c["task_folder"] for c in launch.busy_clients([root])] == ["client"]
    # A dry run never reaches the robot: the client does not stop it.
    assert launch.plan(request(job)).launchable


# --- launching ---------------------------------------------------------------------------------------


def test_systemd_launch_starts_a_transient_unit_without_restart(
    job, fake_systemd, aeri_home, no_tracking
):
    found, handle = launched(job, backend="systemd")
    assert handle.unit == "levi-aeri-r-cli" and handle.state == "started"
    (argv,) = calls(fake_systemd)
    assert argv[:2] == ["--user", "--unit=levi-aeri-r-cli"]
    assert "--collect" in argv
    assert argv[argv.index("KillMode=control-group") - 1] == "-p"
    assert "TimeoutStopSec=120" in argv
    assert not any("Restart" in a for a in argv)
    assert f"--setenv=LEVI_AERI_HOME={aeri_home}" in argv
    command = argv[argv.index("-m") - 1 :]
    assert command[:3] == [sys.executable, "-m", "levi.automatic.runner"]
    assert command[command.index("--plan-sha256") + 1] == found.plan_sha256
    run_dir = Path(command[command.index("--run-dir") + 1])
    assert run_dir == Path(found.run_dir)
    record = json.loads((run_dir / "launch.json").read_text())
    assert record["plan_sha256"] == found.plan_sha256
    assert record["request"]["execution_mode"] == "dry_run"
    assert stat.S_IMODE((run_dir / "launch.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    index = json.loads((aeri_home / "runs" / "r-cli.json").read_text())
    assert index["run_dir"] == str(run_dir) and index["unit"] == "levi-aeri-r-cli"
    # The runner did not start (a fake systemd-run): no journal yet.
    assert not (run_dir / "state_journal.jsonl").exists()


def test_the_run_id_is_used_once(job, fake_systemd):
    launched(job, backend="systemd")
    found = launch.plan(request(job))
    assert "E_RUN_EXISTS" in found.refusals
    with pytest.raises(launch.LaunchRefused) as caught:
        launch.launch(
            request(job), plan_sha256=found.plan_sha256, launch_token=found.launch_token
        )
    assert caught.value.code == "E_RUN_EXISTS"
    assert len(calls(fake_systemd)) == 1


def test_two_launches_at_once_start_one_run(job, fake_systemd):
    found = launch.plan(request(job))
    results, barrier = [], threading.Barrier(4)

    def go():
        barrier.wait()
        try:
            launch.launch(
                request(job),
                plan_sha256=found.plan_sha256,
                launch_token=found.launch_token,
            )
            results.append("ok")
        except launch.LaunchRefused as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(results) == ["E_RUN_EXISTS"] * 3 + ["ok"]
    assert len(calls(fake_systemd)) == 1


def test_a_failed_systemd_run_says_so(job, fake_systemd, monkeypatch, aeri_home):
    monkeypatch.setenv("FAKE_SYSTEMD_FAIL", "1")
    with pytest.raises(launch.LaunchRefused) as caught:
        launched(job, backend="systemd")
    assert caught.value.code == "E_SYSTEMD" and "Failed" in caught.value.detail
    index = json.loads((aeri_home / "runs" / "r-cli.json").read_text())
    assert index["runner"] == {"state": "failed_to_start"}


def test_no_systemd_run_on_path(job, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    with pytest.raises(launch.LaunchRefused) as caught:
        launched(job, backend="systemd")
    assert caught.value.code == "E_SYSTEMD"


def test_a_foreground_dry_run_runs_here_and_completes(job, aeri_home, no_tracking):
    found, handle = launched(job, backend="foreground")
    assert handle.exit_code == 0 and handle.final_state == "COMPLETED"
    run_dir = Path(handle.run_dir)
    scan = Journal.read(run_dir)
    header = scan.events[0].header
    assert header.plan_sha256 == found.plan_sha256
    assert read_plan(run_dir)["plan"] == json.loads(json.dumps(found.plan, default=str))
    record = json.loads((run_dir / "runner.json").read_text())
    assert record["state"] == "exited" and record["final_state"] == "COMPLETED"
    assert record["pid"] == os.getpid() and record["identity"]["start_ticks"]
    assert record["robot_motions"] > 0  # the fake robot of the dry run
    index = json.loads((aeri_home / "runs" / "r-cli.json").read_text())
    assert index["runner"]["final_state"] == "COMPLETED"
    listed = launch.runs()
    assert [r["run_id"] for r in listed] == ["r-cli"]
    assert listed[0]["state"] == "COMPLETED" and not listed[0]["runner_alive"]


def test_an_inprocess_dry_run_keeps_its_files_where_asked(job, tmp_path):
    keep = tmp_path / "kept"
    _, handle = launched(job, backend="inprocess", keep_dir=str(keep))
    assert handle.final_state == "COMPLETED"
    assert Path(handle.run_dir) == keep / ".aeri" / "runs" / "r-cli"
    # Nothing in the job's rollout root: a dry run writes its own folder.
    assert not any((tmp_path / "rollouts").iterdir())


def test_a_keep_folder_that_is_not_empty_is_refused(job, tmp_path):
    keep = tmp_path / "kept"
    keep.mkdir()
    (keep / "x").write_text("x")
    assert "E_KEEP_DIR" in launch.plan(request(job, keep_dir=str(keep))).refusals


# --- the runner's own checks ---------------------------------------------------------------------------


def _prepared(job, fake_systemd, **kw):
    found, handle = launched(job, backend="systemd", **kw)
    return found, Path(handle.run_dir)


def test_the_runner_refuses_a_job_that_changed_after_the_launch(job, fake_systemd):
    found, run_dir = _prepared(job, fake_systemd)
    job.write_text(job.read_text().replace("episodes: 2", "episodes: 4"))
    result = runner.serve(run_dir, found.plan_sha256)
    assert result.exit_code == 2 and result.code == "E_PLAN_CHANGED"
    assert not (run_dir / "state_journal.jsonl").exists()
    assert json.loads((run_dir / "runner.json").read_text())["state"] == "refused"


def test_the_runner_refuses_another_digest(job, fake_systemd):
    _, run_dir = _prepared(job, fake_systemd)
    result = runner.serve(run_dir, "f" * 64)
    assert result.code == "E_PLAN_CHANGED"
    assert not (run_dir / "state_journal.jsonl").exists()


def test_the_runner_refuses_a_real_mode_in_its_launch_record(job, fake_systemd):
    found, run_dir = _prepared(job, fake_systemd)
    record = json.loads((run_dir / "launch.json").read_text())
    record["request"]["execution_mode"] = "autonomous"
    (run_dir / "launch.json").write_text(json.dumps(record))
    result = runner.serve(run_dir, found.plan_sha256)
    assert result.code == "E_NO_ROBOT_ADAPTER" and result.exit_code == 2
    assert not (run_dir / "state_journal.jsonl").exists()


def test_the_runner_refuses_a_launch_record_of_another_folder(
    job, fake_systemd, tmp_path
):
    found, run_dir = _prepared(job, fake_systemd)
    elsewhere = tmp_path / "copy" / ".aeri" / "runs" / "r-cli"
    elsewhere.mkdir(parents=True)
    (elsewhere / "launch.json").write_bytes((run_dir / "launch.json").read_bytes())
    result = runner.serve(elsewhere, found.plan_sha256)
    assert result.code == "E_REQUEST"
    assert not (elsewhere / "state_journal.jsonl").exists()


def test_the_runner_refuses_a_folder_without_a_launch_record(tmp_path):
    result = runner.serve(tmp_path, "0" * 64)
    assert result.code == "E_REQUEST" and result.exit_code == 2


def test_a_second_start_of_a_run_is_refused(job, fake_systemd):
    found, run_dir = _prepared(job, fake_systemd)
    assert runner.serve(run_dir, found.plan_sha256).state == "COMPLETED"
    again = runner.serve(run_dir, found.plan_sha256)
    assert again.code in ("E_RUN_EXISTS",) and again.exit_code == 2
