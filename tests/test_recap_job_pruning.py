"""Job folders stay bounded: a succeeded job's plan and worker output go
after publication, failed jobs keep their diagnosis, old records age out."""

import json
import sys
import time

from test_recap_value import capture, finish, recap, run, store_env  # noqa: F401

from levi.recap import jobs, store


def _files(name, job_id):
    base = store.root(name)
    return {
        "plan": base / "plans" / f"{job_id}.json",
        "result": base / "results" / f"{job_id}.json",
        "values": base / "results" / f"{job_id}.values.parquet",
        "progress": base / "jobs" / f"{job_id}.progress.json",
        "log": base / "jobs" / f"{job_id}.log",
        "record": base / "jobs" / f"{job_id}.json",
    }


def test_succeeded_job_loses_plan_and_output_after_publishing(
    recap,  # noqa: F811
    capture,  # noqa: F811
    monkeypatch,
):
    client, repo = recap, capture["id"]
    job = run(client, repo)
    assert job["status"] == "succeeded", job
    name = jobs.dataset(repo).name
    files = _files(name, job["id"])
    assert not files["plan"].exists()
    assert not files["result"].exists() and not files["values"].exists()
    assert not files["progress"].exists()
    assert files["record"].is_file() and files["log"].exists()
    # The published result is untouched and still served.
    episode = client.get("/annotations/api/recap/episodes/0", params={"repo_id": repo})
    assert episode.status_code == 200 and episode.json()["value"]
    assert jobs.find(job["id"], name)["status"] == "succeeded"

    # A failed job keeps its plan and log for diagnosis.
    monkeypatch.setattr(
        jobs,
        "worker_command",
        lambda provider: [sys.executable, "-c", "print('boom'); raise SystemExit(3)"],
    )
    failed = run(client, repo)
    assert failed["status"] == "failed"
    kept = _files(name, failed["id"])
    assert kept["plan"].is_file() and kept["log"].is_file()
    assert "boom" in kept["log"].read_text()


def _record(name, job_id, status, created):
    store.write_json(
        store.root(name) / "jobs" / f"{job_id}.json",
        {"id": job_id, "name": name, "status": status, "created_at": created},
    )
    files = _files(name, job_id)
    for key in ("plan", "result", "values", "progress", "log"):
        files[key].parent.mkdir(parents=True, exist_ok=True)
        files[key].write_text(key)
    return files


def test_old_records_age_out_and_active_jobs_are_never_touched(client, tmp_path):
    name = "prune-ds"
    ids = [f"20261001-00{i:02d}" for i in range(6)]
    made = {
        ids[0]: _record(name, ids[0], "failed", 1),
        ids[1]: _record(name, ids[1], "succeeded", 2),
        ids[2]: _record(name, ids[2], "running", 3),
        ids[3]: _record(name, ids[3], "cancelled", 4),
        ids[4]: _record(name, ids[4], "failed", 5),
        ids[5]: _record(name, ids[5], "succeeded", 6),
    }
    # Another job's files with a shared prefix are not this job's.
    other = store.root(name) / "results" / f"{ids[5]}0.json"
    other.write_text("other")
    report = jobs.prune_job_files(name, keep=3)
    # Five finished records, three kept: the two oldest finished go entirely.
    assert report["removed_records"] == [ids[0], ids[1]]
    for job_id in ids[:2]:
        assert not any(p.exists() for p in made[job_id].values())
    # The running job keeps everything, though it is older than kept ones.
    assert all(p.exists() for p in made[ids[2]].values())
    # Kept failed/cancelled jobs keep their diagnosis.
    for job_id in (ids[3], ids[4]):
        assert all(p.exists() for p in made[job_id].values())
    # A kept succeeded job keeps its record and log only.
    kept = made[ids[5]]
    assert kept["record"].exists() and kept["log"].exists()
    assert not any(kept[k].exists() for k in ("plan", "result", "values", "progress"))
    assert other.read_text() == "other"
    assert report["bytes"] > 0
    assert [j["id"] for j in jobs.jobs(name)] == ids[2:]


def test_pruning_never_follows_symbolic_links(client, tmp_path):
    name = "prune-links"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "precious.json"
    target.write_text("keep me")
    job_id = "20261001-0100"
    store.write_json(
        store.root(name) / "jobs" / f"{job_id}.json",
        {"id": job_id, "name": name, "status": "succeeded", "created_at": 1},
    )
    results = store.root(name) / "results"
    results.mkdir(parents=True)
    (results / f"{job_id}.json").symlink_to(target)
    plans_outside = outside / "plans"
    plans_outside.mkdir()
    (plans_outside / f"{job_id}.json").write_text("plan elsewhere")
    (store.root(name) / "plans").symlink_to(plans_outside, target_is_directory=True)
    jobs.prune_job_files(name)
    assert target.read_text() == "keep me"
    assert not (results / f"{job_id}.json").is_symlink()
    assert (plans_outside / f"{job_id}.json").read_text() == "plan elsewhere"


def test_a_crash_after_publishing_is_cleaned_by_the_next_pass(client):
    name = "prune-crash"
    job_id = "20261001-0200"
    files = _record(name, job_id, "succeeded", time.time())
    assert json.loads(files["record"].read_text())["status"] == "succeeded"
    jobs.prune_job_files(name)
    assert not files["plan"].exists() and not files["values"].exists()
    assert files["record"].exists()
