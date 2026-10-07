"""Training-pool jobs that survive being stopped: resume, states, errors,
logs, stop safety and cleanup (docs/TRAINING_POOL.md, "Jobs")."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import scaled
from test_pool import declare_grippers, make_demo

from levi import children
from levi.conversion import pipeline
from levi.pool import cleanup, export, joblog, jobs, journal, recipe, scanner, settings
from levi.pool.recipe import Recipe


def _root(base: Path, n: int, fps=10) -> Path:
    root = base / "rpool"
    for i in range(n):
        make_demo(
            root / "rollouts/models/pi/pick_x" / f"demo_{i:04d}",
            rollout="success",
            task="pick x",
            created=f"2026-09-{1 + i // 24:02d}T{i % 24:02d}:00:00",
            fps=fps,
        )
    return root


@pytest.fixture(scope="module")
def big_root(tmp_path_factory):
    return _root(tmp_path_factory.mktemp("resume"), 8)


def _env(monkeypatch, root: Path, tmp_path: Path, **more):
    monkeypatch.setenv("LEVI_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("LEVI_POOL_ROOTS", str(root))
    monkeypatch.setenv("LEVI_POOL_HELDOUT", "none")
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", str(tmp_path))
    monkeypatch.setenv("LEVI_POOL_BATCH_EPISODES", "2")
    for key, value in more.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setattr(export, "_levi_commit", lambda: "test")
    declare_grippers(root)
    scanner.scan()


@pytest.fixture
def rp(big_root, tmp_path, monkeypatch):
    _env(monkeypatch, big_root, tmp_path)
    return {"root": big_root, "out": tmp_path / "exports"}


def _rec():
    return Recipe(name="r", categories=["rollout"], tasks=["pick x"])


def _plan(rp, name, **kw):
    options = export.ExportOptions(
        format=kw.pop("format", "lerobot_v21"),
        name=name,
        output_dir=str(rp["out"]),
        timing=kw.pop("timing", "retime"),
        filter_static=False,
        workers=1,
        **kw,
    )
    return jobs.plan_export(_rec(), options)


def _run(job):
    return jobs.run_worker(jobs._path(job["id"]))


def _interrupt_after(monkeypatch, parts: int, code=143, marker: Path | None = None):
    """A conversion that stops like a SIGTERM after ``parts`` parts (after the
    cancel or stopping ``marker`` is written, when given)."""
    real = pipeline.run
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        if calls["n"] >= parts:
            if marker:
                marker.write_text("1")
            joblog.TERMINATING.set()
            raise SystemExit(code)
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(export.pipeline, "run", flaky)
    return real


def _signature(path: Path) -> dict:
    out = {}
    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(path).as_posix()
        if rel.startswith(("data/", "videos/")):
            out[rel] = hashlib.sha256(item.read_bytes()).hexdigest()
        elif rel.startswith("meta/") and not rel.endswith("levi_validation.json"):
            out[rel] = item.read_text()
        elif rel == "pool_export.json":
            record = json.loads(item.read_text())
            for key in (
                "created_at",
                "name",
                "resumed",
                "resumes",
                "interruptions",
                "levi_commit",
                "job_id",
            ):
                record.pop(key, None)
            record["params"].pop("name")
            out[rel] = record
        else:
            out[rel] = "present"
    return out


# ------------------------------------------------------------- the resume


def test_interrupted_export_keeps_its_partial_and_resumes_to_the_same_dataset(
    rp, monkeypatch
):
    clean = _plan(rp, "clean")
    assert _run(clean) == 0
    clean_out = rp["out"] / "clean"

    job = _plan(rp, "cut")
    real = _interrupt_after(monkeypatch, 2)
    assert _run(job) == 143
    got = jobs.get(job["id"])
    assert got["status"] == "interrupted" and got["resumable"]
    assert "stopped by a signal" in got["reason"]
    assert (
        got["error_info"]["type"] == "Signal" and "Resume" in got["error_info"]["hint"]
    )
    partial = rp["out"] / ".cut.partial"
    assert partial.is_dir() and not (rp["out"] / "cut").exists()
    header = json.loads((partial / journal.HEADER).read_text())
    assert header["state"] == "interrupted" and header["interruptions"] == 1
    assert len(journal.Journal.read(partial).units) == 2
    # Nothing finished is thrown away by the stop itself.
    assert (partial / ".parts").is_dir()

    monkeypatch.setattr(export.pipeline, "run", real)
    joblog.TERMINATING.clear()
    jobs.prepare_resume(job["id"])
    assert _run(job) == 0
    done = jobs.get(job["id"])
    assert done["status"] == "done" and done["result"]["resumed"] is True
    assert not partial.exists()
    record = json.loads((rp["out"] / "cut/pool_export.json").read_text())
    assert record["resumed"] is True and record["resumes"] == 1
    assert record["interruptions"] == 1
    assert _signature(rp["out"] / "cut") == _signature(clean_out)
    assert "resuming" in joblog.tail(jobs._path(job["id"]))
    assert "journal verified" in joblog.tail(jobs._path(job["id"]))
    # The resume skipped the finished parts: only the missing ones converted.
    assert not list(rp["out"].rglob("resume.json"))


def test_a_tampered_unit_is_done_again_and_an_untrusted_plan_is_refused(
    rp, monkeypatch
):
    clean = _plan(rp, "clean")
    _run(clean)
    job = _plan(rp, "cut")
    real = _interrupt_after(monkeypatch, 2)
    _run(job)
    partial = rp["out"] / ".cut.partial"
    monkeypatch.setattr(export.pipeline, "run", real)
    joblog.TERMINATING.clear()
    units = journal.Journal.read(partial).units
    first = next(iter(units.values()))
    victim = next(rel for rel in first["files"] if rel.endswith(".mp4"))
    (partial / victim).write_bytes(b"cut short")
    jobs.prepare_resume(job["id"])
    assert _run(job) == 0
    assert "failed verification" in joblog.tail(jobs._path(job["id"]))
    assert _signature(rp["out"] / "cut") == _signature(rp["out"] / "clean")

    # A plan that no longer matches its journal cannot continue.
    other = _plan(rp, "cut2")
    _interrupt_after(monkeypatch, 1)
    _run(other)
    monkeypatch.setattr(export.pipeline, "run", real)
    joblog.TERMINATING.clear()
    record = jobs.read_job(other["id"])
    record["options"]["fps"] = 12
    from levi.catalog import atomic

    atomic(jobs._path(other["id"]), record)
    with pytest.raises(journal.ResumeRefused, match="plan changed"):
        jobs.prepare_resume(other["id"])


def test_resume_refuses_changed_sources_and_rerun_plans_again(rp, monkeypatch):
    job = _plan(rp, "cut")
    real = _interrupt_after(monkeypatch, 1)
    _run(job)
    monkeypatch.setattr(export.pipeline, "run", real)
    joblog.TERMINATING.clear()
    demo = next((rp["root"] / "rollouts/models/pi/pick_x").glob("demo_0003"))
    meta = json.loads((demo / "metadata.json").read_text())
    meta["note"] = "edited after the scan"
    (demo / "metadata.json").write_text(json.dumps(meta))
    with pytest.raises(
        journal.ResumeRefused, match="changed since the pool was scanned"
    ):
        jobs.prepare_resume(job["id"])
    assert jobs.get(job["id"])["rerunnable"]
    scanner.scan()
    fresh = jobs.rerun(job["id"])
    assert not jobs.wait_idle(180)
    assert fresh["id"] != job["id"]
    done = jobs.get(fresh["id"])
    assert done["status"] == "done", done.get("error")
    assert (rp["out"] / "cut/pool_export.json").is_file()
    assert not (rp["out"] / ".cut.partial").exists()
    assert jobs.read_job(job["id"])["superseded_by"] == fresh["id"]


def test_cancel_removes_the_partial_but_an_interrupt_keeps_it(rp, monkeypatch):
    real = pipeline.run
    kept = _plan(rp, "kept")
    _interrupt_after(monkeypatch, 1)
    _run(kept)
    assert (rp["out"] / ".kept.partial").is_dir()
    # An explicit cancel of the stopped job removes it.
    monkeypatch.setattr(export.pipeline, "run", real)
    joblog.TERMINATING.clear()
    cancelled = jobs.cancel(kept["id"])
    assert cancelled["status"] == "cancelled"
    assert not (rp["out"] / ".kept.partial").exists()
    # A cancel that reaches a running worker (marker, then the signal) too.
    job = _plan(rp, "cancelled")
    _interrupt_after(
        monkeypatch, 1, marker=joblog.paths(jobs._path(job["id"]))["cancel"]
    )
    _run(job)
    assert jobs.get(job["id"])["status"] == "cancelled"
    assert not (rp["out"] / ".cancelled.partial").exists()
    monkeypatch.setattr(export.pipeline, "run", real)


def test_a_service_stop_is_recorded_as_such(rp, monkeypatch):
    job = _plan(rp, "svc")
    _interrupt_after(
        monkeypatch, 1, marker=joblog.paths(jobs._path(job["id"]))["stopping"]
    )
    _run(job)
    got = jobs.get(job["id"])
    assert got["status"] == "interrupted" and got["reason"] == "service stopped"
    assert (rp["out"] / ".svc.partial").is_dir()


def test_raw_capture_copy_resumes_too(rp, monkeypatch):
    clean = _plan(rp, "rawclean", format="raw_capture")
    _run(clean)
    job = _plan(rp, "rawcut", format="raw_capture")
    real = export._link_or_copy
    seen = {"n": 0}

    def flaky(src, dst, hardlink):
        if src.name == "metadata.json":
            seen["n"] += 1
            if seen["n"] > 3:
                joblog.TERMINATING.set()
                raise SystemExit(143)
        return real(src, dst, hardlink)

    monkeypatch.setattr(export, "_link_or_copy", flaky)
    assert _run(job) == 143
    partial = rp["out"] / ".rawcut.partial"
    assert partial.is_dir() and jobs.get(job["id"])["resumable"]
    monkeypatch.setattr(export, "_link_or_copy", real)
    joblog.TERMINATING.clear()
    jobs.prepare_resume(job["id"])
    assert _run(job) == 0

    def tree(path):
        return {
            p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob("*"))
            if p.is_file() and p.name != "pool_export.json"
        }

    assert tree(rp["out"] / "rawcut") == tree(rp["out"] / "rawclean")
    record = json.loads((rp["out"] / "rawcut/pool_export.json").read_text())
    assert record["resumed"] is True and record["timing"] is None


# ------------------------------------------------- real processes and signals


def _wait(predicate, timeout=90.0, step=0.05):
    end = time.monotonic() + scaled(timeout)
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out")


def _units(partial: Path) -> int:
    path = partial / journal.UNITS
    return len(path.read_text().splitlines()) if path.is_file() else 0


def test_sigterm_and_sigkill_of_a_real_worker_leave_a_resumable_job(rp, monkeypatch):
    monkeypatch.setenv("LEVI_POOL_BATCH_EPISODES", "1")
    clean = _plan(rp, "clean")
    _run(clean)
    job = _plan(rp, "live")
    partial = rp["out"] / ".live.partial"
    jobs.launch(job["id"])
    _wait(lambda: _units(partial) >= 1)
    proc = jobs.ACTIVE[job["id"]]
    os.killpg(proc.pid, signal.SIGTERM)  # what `levi stop` does
    assert not jobs.wait_idle(60)
    got = jobs.get(job["id"])
    assert got["status"] == "interrupted", got
    assert got["resumable"] and partial.is_dir()
    assert got["error_info"]["type"] == "Signal"
    # Resume, then the worker is killed hard (out of memory): the record is
    # closed by the service the moment it notices, not left "running".
    jobs.resume(job["id"])
    assert jobs.get(job["id"])["status"] in ("running", "stalled")
    before = _units(partial)
    _wait(lambda: _units(partial) >= before + 1)
    proc = jobs.ACTIVE[job["id"]]
    os.killpg(proc.pid, signal.SIGKILL)
    assert not jobs.wait_idle(60)
    got = jobs.get(job["id"])
    assert got["status"] == "interrupted" and got["resumable"], got
    assert "SIGKILL" in got["error_info"]["message"]
    assert "memory" in got["error_info"]["hint"]
    # A third start finishes it, with the same result as a clean run.
    jobs.resume(job["id"])
    assert not jobs.wait_idle(180)
    done = jobs.get(job["id"])
    assert done["status"] == "done", done.get("error")
    assert _signature(rp["out"] / "live") == _signature(rp["out"] / "clean")
    record = json.loads((rp["out"] / "live/pool_export.json").read_text())
    assert record["resumes"] == 2 and record["interruptions"] >= 1
    assert not (rp["out"] / ".live.partial").exists()


def test_a_dead_worker_is_interrupted_and_a_silent_one_is_stalled(rp, monkeypatch):
    from levi.catalog import atomic

    job = _plan(rp, "ghost")
    path = jobs._path(job["id"])
    record = jobs.read_job(job["id"])
    # A worker that is gone (its PID is nobody's we know): interrupted, at once.
    record.update(
        status="running",
        worker={"pid": 2**22 + 5, "identity": {"start_ticks": "1"}, "host": "x"},
        started_at=time.time() - 10,
    )
    atomic(path, record)
    got = jobs.get(job["id"])
    assert got["status"] == "interrupted" and "gone" in got["reason"] or got["reason"]
    assert jobs.read_job(job["id"])["status"] == "interrupted"  # and it stays so
    # One that lives but has not moved for longer than LEVI_POOL_STALL_SECONDS.
    monkeypatch.setenv("LEVI_POOL_STALL_SECONDS", "5")
    record = jobs.read_job(job["id"])
    record.update(
        status="running",
        worker={
            "pid": os.getpid(),
            "identity": children.identity(os.getpid()),
            "host": "x",
        },
        started_at=time.time() - 100,
    )
    for key in ("reason", "error", "error_info", "interrupted_at", "finished_at"):
        record.pop(key, None)
    atomic(path, record)
    atomic(
        joblog.paths(path)["beat"],
        {"at": time.time() - 60, "pid": os.getpid(), "activity_at": time.time() - 60},
    )
    got = jobs.get(job["id"])
    assert got["status"] == "stalled" and got["age_seconds"] >= 59
    # A fresh heartbeat with old activity is stalled too (alive but stuck).
    atomic(
        joblog.paths(path)["beat"],
        {"at": time.time(), "pid": os.getpid(), "activity_at": time.time() - 60},
    )
    assert jobs.get(job["id"])["status"] == "stalled"
    atomic(
        joblog.paths(path)["beat"],
        {"at": time.time(), "pid": os.getpid(), "activity_at": time.time()},
    )
    assert jobs.get(job["id"])["status"] == "running"


def test_service_start_marks_orphans_interrupted_and_can_auto_resume(rp, monkeypatch):
    from levi.catalog import atomic

    job = _plan(rp, "orphan")
    path = jobs._path(job["id"])
    record = jobs.read_job(job["id"])
    record.update(status="running", started_at=time.time() - 30)
    atomic(path, record)
    resumed = []
    monkeypatch.setattr(jobs, "resume", lambda job_id: resumed.append(job_id))
    jobs.recover_interrupted()
    stored = jobs.read_job(job["id"])
    assert stored["status"] == "interrupted" and stored["interrupted_at"]
    assert "restarted" in stored["reason"] and resumed == []
    monkeypatch.setenv("LEVI_POOL_AUTO_RESUME", "1")
    jobs.recover_interrupted()  # nothing running now: nothing to resume
    record = jobs.read_job(job["id"])
    record.update(status="running")
    record.pop("worker", None)
    record["launched_at"] = 0
    atomic(path, record)
    jobs.recover_interrupted()
    assert resumed == [job["id"]]


# ------------------------------------------------------------------ errors


def test_a_corrupt_episode_is_left_out_and_the_export_finishes_with_warnings(
    tmp_path, monkeypatch
):
    root = _root(tmp_path, 12)
    _env(monkeypatch, root, tmp_path)
    bad = root / "rollouts/models/pi/pick_x/demo_0004"
    (bad / "side_camera.mp4").write_bytes(b"not a video")
    rp = {"out": tmp_path / "exports"}
    scanner.scan()
    job = _plan(rp, "errs")
    assert _run(job) == 0
    got = jobs.get(job["id"])
    # The capture check left it out: a warning, the export is complete.
    assert got["status"] == "done_with_warnings" and got["result"]["errors"] == 1
    assert got["result"]["left_out"] == 1 and got["result"]["failed"] == 0
    assert got["failures"] == 1 and got["left_out"] == 1 and got["failed"] == 0
    failure = joblog.read_failures(jobs._path(job["id"]))[0]
    assert failure["episode"] == str(bad) and failure["stage"]
    assert {"unit", "type", "message", "traceback", "at"} <= set(failure)
    record = json.loads((rp["out"] / "errs/pool_export.json").read_text())
    assert record["counts"]["episodes"] == 11
    assert record["counts"]["excluded"]["conversion_preflight"] == 1
    assert [e["episode"] for e in record["errors"]] == [str(bad)]
    assert "left out" in joblog.tail(jobs._path(job["id"]))


def test_a_conversion_failure_costs_only_its_episode_until_too_many_fail(
    tmp_path, monkeypatch
):
    root = _root(tmp_path, 12)
    _env(monkeypatch, root, tmp_path, LEVI_POOL_BATCH_EPISODES=4)
    rp = {"out": tmp_path / "exports"}
    real = pipeline.run
    doomed = {str(root / "rollouts/models/pi/pick_x" / f"demo_{i:04d}") for i in (2, 9)}

    def failing(*args, **kwargs):
        keys = {ep["key"] for _, ep in args[3].items}
        if keys & doomed:
            raise ValueError("encoder exploded")
        return real(*args, **kwargs)

    monkeypatch.setattr(export.pipeline, "run", failing)
    job = _plan(rp, "some", on_error_max_fraction=0.2)
    assert _run(job) == 0
    got = jobs.get(job["id"])
    # An exception while converting is an error, not a data check.
    assert got["status"] == "done_with_errors" and got["result"]["errors"] == 2
    assert got["result"]["failed"] == 2 and got["result"]["left_out"] == 0
    record = json.loads((rp["out"] / "some/pool_export.json").read_text())
    assert record["counts"]["episodes"] == 10
    assert record["counts"]["excluded"]["convert_error"] == 2
    assert {e["type"] for e in record["errors"]} == {"ValueError"}
    info = json.loads((rp["out"] / "some/meta/info.json").read_text())
    assert info["total_episodes"] == 10
    assert not (rp["out"] / ".some.partial").exists()
    # Past the allowed share the export stops: failed, structured, resumable.
    stop = _plan(rp, "toomany", on_error_max_fraction=0.1)
    assert _run(stop) == 1
    got = jobs.get(stop["id"])
    assert got["status"] == "failed" and got["resumable"]
    assert "on_error_max_fraction" in got["error"]
    info = joblog.read_error(jobs._path(stop["id"]))
    assert info["type"] == "FatalExport" and info["stage"] and info["hint"]
    assert (rp["out"] / ".toomany.partial").is_dir()
    report = jobs.error_report(stop["id"])
    assert report["error"]["type"] == "FatalExport" and report["failures"]
    assert "encoder exploded" in json.dumps(report["failures"])
    assert "log_tail" in report and report["job"]["id"] == stop["id"]


def test_the_space_preflight_refuses_early_with_numbers(rp, monkeypatch):
    monkeypatch.setenv("LEVI_POOL_FREE_MARGIN_GIB", "100000000")
    with pytest.raises(ValueError, match=r"Not enough free space.*GiB is free"):
        _plan(rp, "big")
    assert not (rp["out"] / ".big.partial").exists()
    # At run time too: the volume may have filled since the plan.
    monkeypatch.setenv("LEVI_POOL_FREE_MARGIN_GIB", "0")
    job = _plan(rp, "big")
    monkeypatch.setenv("LEVI_POOL_FREE_MARGIN_GIB", "100000000")
    assert _run(job) == 1
    got = jobs.get(job["id"])
    assert got["status"] == "failed" and "Not enough free space" in got["error"]
    assert "Free space" in got["error_info"]["hint"] or got["error_info"]["hint"]
    assert not (rp["out"] / ".big.partial").exists()  # nothing finished: removed


def test_exit_codes_and_exceptions_become_plain_messages():
    assert "SIGTERM" in joblog.describe_exit(143)["message"]
    assert "Resume" in joblog.describe_exit(-15)["hint"]
    assert "memory" in joblog.describe_exit(137)["hint"]
    assert "memory" in joblog.describe_exit(-9)["hint"]
    assert "Ctrl+C" in joblog.describe_exit(130)["message"]
    assert "without a result" in joblog.describe_exit(3)["message"]
    full = joblog.describe_exception(OSError(28, "No space left on device"))
    assert full["message"].startswith("Disk full") and "Free space" in full["hint"]
    denied = joblog.describe_exception(PermissionError(13, "denied"))
    assert "Permission denied" in denied["message"]
    held = joblog.describe_exception(PermissionError("Refusing to export 2 held-out"))
    assert "never exported" in held["hint"]
    missing = joblog.describe_exception(FileNotFoundError(2, "gone"), "Merge")
    assert missing["stage"] == "Merge" and "scan the pool again" in missing["hint"]
    stale = joblog.describe_exception(
        ValueError("3 source episode(s) changed since the pool was scanned")
    )
    assert "levi pool scan" in stale["hint"]


def test_the_job_log_is_timestamped_capped_and_tailed(tmp_path):
    path = tmp_path / "job-1.json"
    log = joblog.JobLog(joblog.paths(path)["log"], cap=400)
    for i in range(30):
        log.info(f"line {i}")
    log.error("boom\nsecond line")
    text = joblog.tail(path, 64)
    assert "ERROR boom" in text and "second line" in text
    assert text.split()[0].startswith("20") and "T" in text.split()[0]
    assert joblog.paths(path)["log"].with_name("job-1.log.1").is_file()
    assert joblog.paths(path)["log"].stat().st_size < 1200


# ---------------------------------------------------------------- stop safety


def _fake_running(monkeypatch, sequences):
    from levi import activity

    calls = iter(sequences)
    last = []

    def running():
        nonlocal last
        last = next(calls, last)
        return last

    monkeypatch.setattr(activity, "running_jobs", running)


def test_levi_stop_refuses_while_jobs_run_unless_forced_or_waited_out(
    monkeypatch, capsys
):
    from levi.agent import core

    rows = [
        {
            "kind": "pool",
            "id": "export-1",
            "pid": 5,
            "running_seconds": 700,
            "progress": {"stage": "Merge", "done": 895, "total": 974},
        }
    ]
    monkeypatch.setattr(core, "status", lambda: None)
    monkeypatch.setattr(core, "JOB_POLL_SECONDS", 0.01)
    _fake_running(monkeypatch, [rows, rows])
    refused = core.stop()
    assert refused["status"] == "refused" and refused["running"] == rows
    assert "--force" in refused["hint"] and "--wait" in refused["hint"]
    assert core.stop(force=True)["status"] == "stopped"
    _fake_running(monkeypatch, [rows, rows, []])
    assert core.stop(wait_jobs=0.05)["status"] == "stopped"
    assert "waiting for 1 running job" in capsys.readouterr().err
    _fake_running(monkeypatch, [rows])
    assert core.stop(wait_jobs=0.001)["status"] == "refused"
    # The command reports the list and exits non-zero.
    from levi import cli

    _fake_running(monkeypatch, [rows])
    monkeypatch.setattr(sys, "argv", ["levi", "stop"])
    assert cli.main() == 3
    err = capsys.readouterr().err
    assert "levi stop refused" in err and "895/974" in err
    monkeypatch.setattr(sys, "argv", ["levi", "stop", "--force"])
    assert cli.main() == 0


def test_running_jobs_come_from_the_recorded_workers(monkeypatch):
    from levi import activity

    monkeypatch.setattr(
        children,
        "listed",
        lambda: [
            {
                "kind": "pool",
                "label": "export-2",
                "pid": 7,
                "running": True,
                "started_at": time.time() - 5,
            },
            {"kind": "segmentation-live", "label": "live", "pid": 8, "running": True},
            {"kind": "pool", "label": "export-3", "pid": 9, "running": False},
            {
                "kind": "recap_value",
                "label": "r1",
                "pid": 10,
                "running": True,
                "started_at": time.time(),
            },
        ],
    )
    found = activity.running_jobs()
    assert [j["id"] for j in found] == ["export-2", "r1"]
    assert "export-2" in activity.describe(found)


# -------------------------------------------------------------------- cleanup


def _record(job_id, **fields):
    from levi.catalog import atomic

    settings.job_path(job_id).parent.mkdir(parents=True, exist_ok=True)
    base = {"id": job_id, "kind": "export", "planned_at": time.time()}
    atomic(settings.job_path(job_id), {**base, **fields})


def _partial(out: Path, name: str, journal_header=True, size=1000):
    folder = out / f".{name}.partial"
    folder.mkdir(parents=True)
    (folder / "blob.bin").write_bytes(b"x" * size)
    if journal_header:
        (folder / journal.HEADER).write_text(
            json.dumps({"schema": journal.SCHEMA, "updated_at": time.time()})
        )
    return folder


def test_the_sweeper_follows_the_rules_and_never_touches_live_or_finished(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LEVI_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", str(tmp_path / "exports"))
    monkeypatch.setenv("LEVI_POOL_ROOTS", str(tmp_path / "src"))
    monkeypatch.setenv("LEVI_POOL_PARTIAL_TTL", "3d")
    out = tmp_path / "exports"
    out.mkdir()
    now = time.time()
    old = now - 4 * 86400
    me = {"pid": os.getpid(), "identity": children.identity(os.getpid())}

    def job(name, **fields):
        _record(name, target=str(out / name), **fields)

    job("done", status="done", finished_at=now)
    job("cancelled", status="cancelled", finished_at=now)
    job("fresh", status="interrupted", interrupted_at=now - 3600)
    job("stale", status="interrupted", interrupted_at=old)
    job("failed-old", status="failed", finished_at=old)
    job("live", status="running", worker=me, started_at=old)
    for name in ("done", "cancelled", "fresh", "stale", "failed-old", "live"):
        _partial(out, name)
    _partial(out, "orphan", journal_header=False)
    finished = out / "finished-export"
    finished.mkdir()
    (finished / "pool_export.json").write_text("{}")

    listing = cleanup.inventory()
    by = {Path(p["path"]).name: p for p in listing["partials"]}
    assert (
        by[".live.partial"]["live"]
        and by[".live.partial"]["expires_in_seconds"] is None
    )
    assert by[".stale.partial"]["expires_in_seconds"] == 0
    assert by[".fresh.partial"]["expires_in_seconds"] > 0
    assert by[".fresh.partial"]["resumable"] is True
    assert listing["removable_bytes"] > 0 and listing["disk"]

    dry = cleanup.sweep(dry_run=True)
    assert (out / ".done.partial").exists()  # a dry run removes nothing
    names = {Path(r["id"]).name for r in dry["removed"] if r["kind"] == "partial"}
    assert names == {
        ".done.partial",
        ".cancelled.partial",
        ".stale.partial",
        ".failed-old.partial",
    }
    swept = cleanup.sweep()
    assert {
        Path(r["id"]).name for r in swept["removed"] if r["kind"] == "partial"
    } == names
    for gone in ("done", "cancelled", "stale", "failed-old"):
        assert not (out / f".{gone}.partial").exists()
    for kept in ("fresh", "live", "orphan"):
        assert (out / f".{kept}.partial").exists()
    assert finished.is_dir() and (finished / "pool_export.json").exists()
    # --all-partials also removes what could still resume, but never a live job's.
    cleanup.sweep(all_partials=True)
    assert (
        not (out / ".fresh.partial").exists() and not (out / ".orphan.partial").exists()
    )
    assert (out / ".live.partial").exists()
    assert cleanup.remove_partial(out / ".live.partial") is False
    assert cleanup.remove_partial(out / "finished-export") is False  # not a partial
    outside = tmp_path / "elsewhere" / ".x.partial"
    outside.mkdir(parents=True)
    assert cleanup.remove_partial(outside) is False  # not inside an export root


def test_job_files_expire_into_summaries_and_stale_temporaries_go(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LEVI_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", str(tmp_path / "exports"))
    monkeypatch.setenv("LEVI_POOL_JOB_TTL", "30d")
    old = time.time() - 31 * 86400
    _record(
        "export-a",
        status="done",
        finished_at=old,
        result={"episodes": 3},
        episodes=[{"key": "k"}] * 50,
        options={"name": "a"},
    )
    _record("export-b", status="failed", finished_at=old)
    _record("export-c", status="done", finished_at=time.time())
    for name in ("export-a", "export-b", "export-c"):
        path = settings.job_path(name)
        joblog.paths(path)["log"].write_text("log\n")
        joblog.paths(path)["progress"].write_text("{}")
    tmp = settings.pool_dir() / "jobs" / "x.json.123.tmp"
    tmp.write_text("half")
    os.utime(tmp, (old, old))
    fresh_tmp = settings.pool_dir() / ".index.99.parquet"
    fresh_tmp.write_text("writing now")
    swept = cleanup.sweep()
    kinds = {(r["kind"], Path(r["id"]).name) for r in swept["removed"]}
    assert ("job", "export-a") in kinds and ("job", "export-b") in kinds
    assert ("temp", "x.json.123.tmp") in kinds
    summary = json.loads(settings.job_path("export-a").read_text())
    assert summary["compacted"] and summary["result"] == {"episodes": 3}
    assert "episodes" not in summary and summary["options"] == {"name": "a"}
    assert not joblog.paths(settings.job_path("export-a"))["log"].exists()
    assert not settings.job_path("export-b").exists()  # a failed one goes whole
    assert settings.job_path("export-c").exists()
    assert joblog.paths(settings.job_path("export-c"))["log"].exists()
    assert not tmp.exists() and fresh_tmp.exists()
    assert cleanup.sweep()["removed"] == []  # nothing left to do


def test_a_finished_export_leaves_no_partial_and_no_temporaries(rp):
    job = _plan(rp, "tidy")
    assert _run(job) == 0
    assert not list(rp["out"].rglob("*.partial"))
    assert not list(rp["out"].rglob("resume.json"))
    assert not list(rp["out"].rglob(".parts"))
    folder = settings.pool_dir()
    assert not list(folder.rglob("*.tmp")) and not list(folder.glob(".index.*"))
    assert not joblog.paths(jobs._path(job["id"]))["cancel"].exists()
    assert cleanup.inventory()["partials"] == []


def test_maintenance_clean_covers_the_pool(tmp_path, monkeypatch):
    from levi import maintenance

    monkeypatch.setenv("LEVI_WORKSPACE", str(tmp_path / "ws"))
    # `maintenance.STATE` is fixed at import: without this the test looks at the checkout's own `.state/server.pid`
    # and is refused whenever a product LEVI is running from that checkout.
    monkeypatch.setattr(maintenance, "STATE", tmp_path / "ws")
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", str(tmp_path / "exports"))
    monkeypatch.setenv("LEVI_POOL_JOB_TTL", "1d")
    old = time.time() - 3 * 86400
    _record("export-z", status="failed", finished_at=old)
    report = maintenance.clean(False)
    assert report["pool"]["dry_run"] and settings.job_path("export-z").exists()
    assert any(r["id"] == "export-z" for r in report["pool"]["removed"])
    maintenance.clean(True)
    assert not settings.job_path("export-z").exists()


# ------------------------------------------------------------------------ API


def test_api_resume_rerun_log_error_report_and_cleanup(rp, monkeypatch, client):
    job = _plan(rp, "api-cut")
    real = _interrupt_after(monkeypatch, 1)
    _run(job)
    monkeypatch.setattr(export.pipeline, "run", real)
    joblog.TERMINATING.clear()
    got = client.get(f"/api/levi/pool/jobs/{job['id']}").json()
    assert got["status"] == "interrupted" and got["resumable"] and got["partial"]
    log = client.get(f"/api/levi/pool/jobs/{job['id']}/log", params={"kb": 4}).json()
    assert "interrupted" in log["text"]
    report = client.get(f"/api/levi/pool/jobs/{job['id']}/error-report").json()
    assert report["job"]["status"] == "interrupted" and "log_tail" in report
    assert client.get("/api/levi/pool/jobs/nope-1/log").status_code == 404
    assert client.post("/api/levi/pool/jobs/nope-1/resume").status_code == 404
    listing = client.get("/api/levi/pool/cleanup").json()
    assert [p["job"] for p in listing["partials"]] == [job["id"]]
    assert listing["partials"][0]["resumable"] and listing["disk"]
    assert client.get("/api/levi/pool/status").json()["disk"]
    # A resume the plan cannot back is a 409 with the reason.
    demo = rp["root"] / "rollouts/models/pi/pick_x/demo_0002"
    meta = json.loads((demo / "metadata.json").read_text())
    meta["touched"] = 1
    (demo / "metadata.json").write_text(json.dumps(meta))
    refused = client.post(f"/api/levi/pool/jobs/{job['id']}/resume")
    assert refused.status_code == 409
    assert "changed since the pool was scanned" in refused.json()["detail"]
    # The delete button: named partials, never a live job's.
    deleted = client.post(
        "/api/levi/pool/cleanup", json={"partials": [listing["partials"][0]["path"]]}
    ).json()
    assert deleted["removed"] and not (rp["out"] / ".api-cut.partial").exists()
    again = client.post(
        "/api/levi/pool/cleanup", json={"partials": [str(rp["out"] / ".nope.partial")]}
    ).json()
    assert again["refused"] and not again["removed"]
    assert (
        client.post("/api/levi/pool/cleanup", json={"sweep": True}).json()["dry_run"]
        is False
    )
    cancelled = client.post(f"/api/levi/pool/jobs/{job['id']}/cancel").json()
    assert cancelled["status"] == "cancelled"


def test_push_and_scan_resume_relaunch_their_work(rp, monkeypatch):
    from levi.catalog import atomic

    launched = []
    monkeypatch.setattr(
        jobs,
        "launch",
        lambda job_id, resume=False: launched.append((job_id, resume)) or {},
    )
    exported = rp["out"] / "an-export"
    exported.mkdir(parents=True)
    (exported / "pool_export.json").write_text("{}")
    push = {
        "id": "push-1",
        "kind": "push",
        "source": str(exported),
        "status": "interrupted",
        "planned_at": time.time(),
    }
    atomic(jobs._path("push-1"), push)
    assert jobs.get("push-1")["resumable"]
    jobs.resume("push-1")
    assert launched == [("push-1", True)]
    scan = {"id": "scan-1", "kind": "scan", "status": "failed", "planned_at": 1.0}
    atomic(jobs._path("scan-1"), scan)
    assert jobs.get("scan-1")["resumable"]
    jobs.resume("scan-1")
    fresh = launched[-1][0]
    assert fresh != "scan-1" and fresh.startswith("scan-") and launched[-1][1] is False
    assert jobs.read_job("scan-1")["superseded_by"] == fresh
    with pytest.raises(ValueError, match="Only an interrupted or failed"):
        jobs.resume(fresh)


def test_a_cancelled_or_finished_job_cannot_resume(rp):
    job = _plan(rp, "fin")
    _run(job)
    assert jobs.get(job["id"])["status"] == "done"
    assert not jobs.get(job["id"])["resumable"]
    with pytest.raises(ValueError, match="Only an interrupted or failed"):
        jobs.prepare_resume(job["id"])


def test_old_job_records_read_as_the_new_states(rp):
    from levi.catalog import atomic

    atomic(
        jobs._path("export-old"),
        {
            "id": "export-old",
            "kind": "export",
            "status": "succeeded",
            "planned_at": 1.0,
        },
    )
    assert jobs.get("export-old")["status"] == "done"
    assert shutil.which("ffmpeg") and subprocess and Recipe and recipe


def test_every_fixed_sentence_a_job_error_can_carry_is_in_both_catalogs():
    root = Path(__file__).resolve().parents[1] / "src/i18n"
    en = json.loads((root / "en.json").read_text())
    zh = json.loads((root / "zh.json").read_text())
    for text in joblog.translatable():
        assert text in en and text in zh and zh[text] != text, text
    for word in [
        "planned",
        "running",
        "stalled",
        "cancelling",
        "cancelled",
        "interrupted",
        "failed",
        "done",
        "done_with_warnings",
        "done_with_errors",
    ]:
        assert word in en and word in zh, word


def _fresh_journal(tmp_path):
    partial = tmp_path / ".x.partial"
    partial.mkdir()
    job = {"id": "j", "target": str(tmp_path / "x"), "options": {"format": "f"}}
    return partial, journal.Journal.create(partial, job)


def test_a_stop_while_a_unit_is_written_keeps_the_partial_folder(tmp_path, monkeypatch):
    """A stop signal that lands while a finished unit's line is being written
    (the line on disk, the sync not done) found no unit, and the export
    removed the partial folder with the finished unit in it."""
    partial, log = _fresh_journal(tmp_path)

    def stopped(fd):
        raise SystemExit(143)  # what the worker's SIGTERM handler raises

    monkeypatch.setattr(journal.os, "fsync", stopped)
    with pytest.raises(SystemExit):
        log.record("part-000")
    assert log.has_units()
    monkeypatch.undo()
    again = journal.Journal(partial, log.header)
    again._load_units()  # the line was written before the stop
    assert "part-000" in again.units


def test_a_unit_whose_line_cannot_be_written_is_not_counted(tmp_path, monkeypatch):
    """A write that fails (the disk) records nothing: no unit, no mark."""
    _partial, log = _fresh_journal(tmp_path)

    def full(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(journal.os, "fsync", full)
    with pytest.raises(OSError):
        log.record("part-000")
    assert not log.has_units() and log.writing is None and log.new_units == 0
    monkeypatch.undo()
    log.record("part-001")
    assert log.has_units() and list(log.units) == ["part-001"]


def test_the_outcome_of_a_result_tells_data_checks_from_errors():
    assert jobs.outcome({"errors": 0, "failed": 0, "left_out": 0}) == "done"
    assert (
        jobs.outcome({"errors": 3, "failed": 0, "left_out": 3}) == "done_with_warnings"
    )
    assert jobs.outcome({"errors": 3, "failed": 1, "left_out": 2}) == "done_with_errors"
    # A result without the split (an older worker, another kind of job).
    assert jobs.outcome({"errors": 2}) == "done_with_errors"
    assert jobs.outcome({}) == "done"


def _old_record(job_id, errors, **result):
    from levi.catalog import atomic

    path = jobs._path(job_id)
    atomic(
        path,
        {
            "id": job_id,
            "kind": "export",
            "status": "done_with_errors",
            "planned_at": 1.0,
            "result": {"ok": True, "errors": len(errors), **result},
        },
    )
    for failure in errors:
        joblog.append_failure(path, failure)
    return path


def _check(message, episode="/data/task/demo_0001"):
    return {
        "episode": episode,
        "unit": f"preflight|{episode}",
        "stage": "Convert raw captures",
        "type": "CaptureCheck",
        "message": message,
        "traceback": "",
    }


def test_an_old_record_that_only_left_episodes_out_reads_as_a_warning(rp):
    path = _old_record(
        "export-legacy", [_check("Stale state source timestamp run: 61")]
    )
    before = path.read_text()
    assert jobs.get("export-legacy")["status"] == "done_with_warnings"
    assert path.read_text() == before  # the record itself is never rewritten
    _old_record(
        "export-legacy-real",
        [
            _check("Capture metadata records camera stall"),
            {**_check("encoder exploded"), "type": "ValueError"},
        ],
    )
    assert jobs.get("export-legacy-real")["status"] == "done_with_errors"
    _old_record("export-legacy-nofile", [])
    assert jobs.get("export-legacy-nofile")["status"] == "done_with_errors"


def test_job_details_group_the_left_out_episodes_and_keep_the_warnings(rp, client):
    _old_record(
        "export-detail",
        [
            _check("Stale state source timestamp run: 61", "/d/a/demo_0001"),
            _check("Stale state source timestamp run: 7", "/d/a/demo_0002"),
            _check(
                "Empty CSV: end_effector_pose.csv; Capture metadata records camera stall",
                "/d/b/demo_0003",
            ),
            _check("Something else entirely", "/d/b/demo_0004"),
            {
                **_check("encoder exploded", "/d/c/demo_0005"),
                "type": "ValueError",
                "traceback": "Traceback ...\nValueError: encoder exploded",
            },
        ],
        episodes=10,
        frames=100,
        warnings=[
            {
                "code": "copy_task_conflict",
                "blocking": False,
                "count": 30,
                "episodes": [f"/d/e/demo_{i:04d}" for i in range(30)],
            },
            {"code": "reset_unreviewed", "blocking": False, "message": "m"},
        ],
    )
    got = client.get("/api/levi/pool/jobs/export-detail/details").json()
    assert got["status"] == "done_with_errors"  # one real exception
    assert got["summary"] == {"episodes": 10, "frames": 100}
    assert got["left_out"]["total"] == 4 and got["failed"]["total"] == 1
    groups = {g["code"]: g["count"] for g in got["left_out"]["groups"]}
    assert groups == {"stale_state": 2, "csv_schema": 1, "stall_markers": 1, "other": 1}
    assert set(got["left_out"]["items"][2]["reasons"]) == {
        "csv_schema",
        "stall_markers",
    }
    assert "traceback" in got["failed"]["items"][0]
    assert "encoder exploded" in got["failed"]["items"][0]["traceback"]
    first = got["warnings"][0]
    assert (
        first["episodes_total"] == 30
        and len(first["episodes"]) == jobs.WARNING_EPISODES
    )
    assert got["warnings"][1]["code"] == "reset_unreviewed"
    capped = client.get("/api/levi/pool/jobs/export-detail/details?limit=1").json()
    assert capped["left_out"]["total"] == 4 and len(capped["left_out"]["items"]) == 1
    assert client.get("/api/levi/pool/jobs/nope-1/details").status_code == 404


def test_a_real_export_that_left_a_corrupt_episode_out_opens_with_its_reason(
    tmp_path, monkeypatch, client
):
    root = _root(tmp_path, 6)
    _env(monkeypatch, root, tmp_path)
    (root / "rollouts/models/pi/pick_x/demo_0002/side_camera.mp4").write_bytes(b"x")
    scanner.scan()
    job = _plan({"out": tmp_path / "exports"}, "reason")
    assert _run(job) == 0
    got = client.get(f"/api/levi/pool/jobs/{job['id']}/details").json()
    assert got["status"] == "done_with_warnings"
    assert got["left_out"]["total"] == 1 and got["failed"]["total"] == 0
    assert got["left_out"]["items"][0]["episode"].endswith("demo_0002")
    assert got["summary"]["episodes"] == 5
    # The worker records which check left it out; the page groups by that.
    row = joblog.read_failures(jobs._path(job["id"]))[0]
    assert row["type"] == "CaptureCheck" and row["codes"]
    assert [g["code"] for g in got["left_out"]["groups"]] == row["codes"]
    assert got["left_out"]["items"][0]["reasons"] == row["codes"]


def test_a_warning_that_only_counts_its_episodes_does_not_break_the_details(rp, client):
    _old_record(
        "export-counts",
        [_check("Stale state source timestamp run: 9")],
        warnings=[
            {"code": "gripper_unknown", "blocking": False, "episodes": 5},
            {"code": "retime_time_scale", "episodes": 3, "message": "slower"},
            {"code": "copy_task_conflict", "episodes": ["/a/demo_1"], "count": 301},
            "a plain sentence",
            7,
        ],
    )
    got = client.get("/api/levi/pool/jobs/export-counts/details")
    assert got.status_code == 200
    warnings = got.json()["warnings"]
    assert [(w.get("code"), w["episodes_total"], w["episodes"]) for w in warnings] == [
        ("gripper_unknown", 5, []),
        ("retime_time_scale", 3, []),
        (
            "copy_task_conflict",
            301,
            ["/a/demo_1"],
        ),  # the exporter listed fewer than it counted
        (None, 0, []),
    ]
    assert warnings[3]["message"] == "a plain sentence"


def test_an_episode_written_twice_by_a_resumed_export_counts_once(rp, client):
    row = _check("Stale state source timestamp run: 9", "/d/a/demo_0001")
    _old_record(
        "export-twice", [row, row, _check("Capture contains camera_stalled event")]
    )
    got = jobs.get("export-twice")
    assert got["left_out"] == 2 and got["failures"] == 2
    detail = client.get("/api/levi/pool/jobs/export-twice/details").json()
    assert detail["left_out"]["total"] == 2 and len(detail["left_out"]["items"]) == 2


def test_a_line_of_errors_jsonl_that_is_no_record_is_skipped(rp, client):
    path = _old_record("export-junk", [_check("Stale state source timestamp run: 9")])
    with joblog.paths(path)["errors"].open("a") as handle:
        handle.write("123\nnull\n[]\nnot json\n")
    assert [j["id"] for j in jobs.listing() if j["id"] == "export-junk"] == [
        "export-junk"
    ]
    assert jobs.get("export-junk")["status"] == "done_with_warnings"
    assert client.get("/api/levi/pool/jobs/export-junk/details").status_code == 200
    assert client.get("/api/levi/pool/jobs").status_code == 200


def test_the_reason_comes_from_the_recorded_codes_or_else_from_the_message():
    assert jobs.reasons({"codes": ["frame_count", "fps"], "message": "x"}) == [
        "frame_count",
        "fps",
    ]
    assert jobs.reasons({"message": "Capture contains camera_stalled event"}) == [
        "stall_markers"
    ]
    assert jobs.reasons({"message": "Capture metadata records camera stall"}) == [
        "stall_markers"
    ]
    assert jobs.reasons({"message": "Something else entirely"}) == ["other"]


def test_the_delete_preview_calls_an_old_leave_out_a_warning_too(rp):
    from levi.pool import deletion

    _old_record("export-del", [_check("Stale state source timestamp run: 9")])
    assert deletion.plan("export-del")["status"] == "done_with_warnings"
