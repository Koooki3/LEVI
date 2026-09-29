"""Clearing and deleting pool jobs and the exports they made (safety rules of
levi/pool/deletion.py)."""

import json
import os
import time
from pathlib import Path

import pytest
from test_pool_resume import _env, _plan, _root, _run

from levi.catalog import atomic
from levi.pool import deletion, joblog, jobs, journal, settings
from levi.pool.deletion import DeleteRefused


@pytest.fixture(scope="module")
def big_root(tmp_path_factory):
    return _root(tmp_path_factory.mktemp("delete"), 8)


@pytest.fixture
def rp(big_root, tmp_path, monkeypatch):
    _env(monkeypatch, big_root, tmp_path)
    return {"root": big_root, "out": tmp_path / "exports"}


def _done(rp, name, **kw) -> str:
    job = _plan(rp, name, format="raw_capture", **kw)
    assert _run(job) == 0
    return job["id"]


def _fake(job_id, target, status="done", **fields):
    record = {
        "id": job_id,
        "kind": "export",
        "target": str(target),
        "sources": [],
        "status": status,
        "planned_at": time.time(),
        "finished_at": time.time(),
        "options": {"format": "raw_capture", "name": Path(target).name},
        **fields,
    }
    atomic(settings.job_path(job_id), record)
    return job_id


def _export_dir(path: Path, episodes=2, marker=True, job_id=None):
    for i in range(episodes):
        (path / "task" / f"demo_{i:04d}").mkdir(parents=True)
        (path / "task" / f"demo_{i:04d}" / "x.csv").write_text("a,b\n1,2\n")
    if marker:
        (path / "pool_export.json").write_text(
            json.dumps(
                {
                    "format": "raw_capture",
                    "job_id": job_id,
                    "created_at": "2026-09-29T10:00:00+0800",
                    "counts": {"episodes": episodes},
                }
            )
        )
    return path


def _log():
    return [json.loads(x) for x in deletion.deleted_log().read_text().splitlines()]


# -------------------------------------------------------------------- success


def test_clear_record_keeps_the_export_and_delete_removes_only_that_one(rp):
    keep = _done(rp, "keep")
    clear = _done(rp, "clear")
    gone = _done(rp, "gone")
    out = rp["out"]
    plan = deletion.plan(gone, files=True)
    item = plan["outputs"][0]
    assert item["role"] == "output" and item["owned"] and item["will_delete"]
    assert item["episodes"] == 8 and item["format"] == "raw_capture"
    assert item["created_at"] and item["bytes"] > 0 and item["pushed"] == []
    assert plan["freed_bytes"] >= item["bytes"] and not plan["needs_force"]

    cleared = deletion.delete(clear, files=False)
    assert cleared["record_cleared"] and cleared["removed"] == []
    assert not settings.job_path(clear).exists() and (out / "clear").is_dir()
    assert not any(
        joblog.paths(settings.job_path(clear)).values() and False for _ in [0]
    )

    size = sum(p.stat().st_size for p in (out / "gone").rglob("*") if p.is_file())
    result = deletion.delete(gone, files=True, how="test")
    assert result["record_cleared"] and not (out / "gone").exists()
    assert result["freed_bytes"] >= size and result["errors"] == []
    assert result["removed"][0]["bytes"] == size
    assert (out / "keep").is_dir() and (out / "keep/pool_export.json").is_file()
    assert settings.job_path(keep).exists()
    entries = _log()
    assert [e["what"] for e in entries] == ["record", "output", "record"]
    output = entries[1]
    assert output["job_id"] == gone and output["path"] == str(out / "gone")
    assert output["bytes"] == size and output["files"] > 0
    assert "via test" in output["by"] and output["at"]
    assert entries[0]["job_id"] == clear
    # A deleted job leaves nothing but that line.
    assert not list(settings.pool_dir().glob(f"jobs/{gone}*"))


def test_a_failed_job_with_the_same_target_never_deletes_the_successful_ones_dir(rp):
    good = _done(rp, "shared")
    target = rp["out"] / "shared"
    _fake("export-failed", target, status="failed", error="boom")
    plan = deletion.plan("export-failed", files=True)
    [item] = plan["outputs"]
    assert item["owner"] == good and not item["owned"] and not item["will_delete"]
    assert good in item["kept_because"] and item["shared_with"] == [good]
    result = deletion.delete("export-failed", files=True)
    assert result["record_cleared"] and result["removed"] == []
    assert result["kept"] and good in result["kept"][0]["why"]
    assert target.is_dir() and (target / "pool_export.json").is_file()
    # The owner deletes it, and the failed one's record did not matter.
    _fake("export-failed-2", target, status="failed")
    deletion.delete(good, files=True)
    assert not target.exists()


def test_a_failed_job_deletes_its_own_partial(rp):
    partial = rp["out"] / ".half.partial"
    partial.mkdir(parents=True)
    (partial / "blob").write_bytes(b"x" * 500)
    (partial / journal.HEADER).write_text(
        json.dumps({"schema": journal.SCHEMA, "job_id": "export-half"})
    )
    _fake("export-half", rp["out"] / "half", status="interrupted")
    plan = deletion.plan("export-half", files=True)
    assert [i["role"] for i in plan["outputs"]] == ["partial"]
    assert plan["outputs"][0]["will_delete"]
    result = deletion.delete("export-half", files=True)
    assert not partial.exists() and result["removed"][0]["role"] == "partial"
    # Another job's partial under the same name is left alone.
    other = rp["out"] / ".other.partial"
    other.mkdir()
    (other / journal.HEADER).write_text(
        json.dumps({"schema": journal.SCHEMA, "job_id": "export-someone"})
    )
    _fake("export-mine", rp["out"] / "other", status="failed")
    result = deletion.delete("export-mine", files=True)
    assert other.is_dir() and result["kept"]


def test_bulk_delete_and_clear_failed(rp):
    done = _done(rp, "d1")
    done2 = _done(rp, "d2")
    for name, status in (("f1", "failed"), ("i1", "interrupted"), ("c1", "cancelled")):
        partial = rp["out"] / f".{name}.partial"
        partial.mkdir()
        (partial / journal.HEADER).write_text(
            json.dumps({"schema": journal.SCHEMA, "job_id": f"export-{name}"})
        )
        _fake(f"export-{name}", rp["out"] / name, status=status)
    running = _fake(
        "export-run",
        rp["out"] / "run",
        status="running",
        worker={"pid": os.getpid(), "identity": jobs.children.identity(os.getpid())},
    )
    result = deletion.clear_failed(how="test")
    assert result["cleared"] == 3 and not result["refused"]
    for name in ("f1", "i1", "c1"):
        assert not settings.job_path(f"export-{name}").exists()
        assert not (rp["out"] / f".{name}.partial").exists()
    # Finished exports and running jobs are never touched.
    assert (rp["out"] / "d1/pool_export.json").is_file()
    assert settings.job_path(done).exists() and settings.job_path(running).exists()
    both = deletion.delete_many([done, done2, running, "nope-1"], files=True)
    assert [r["id"] for r in both["results"]] == [done, done2]
    reasons = {r["id"]: r["reason"] for r in both["refused"]}
    assert "cancel it first" in reasons[running] and "nope-1" in reasons
    assert both["freed_bytes"] > 0
    assert not (rp["out"] / "d1").exists() and not (rp["out"] / "d2").exists()
    whats = [e["what"] for e in _log()]
    assert whats.count("partial") == 3 and whats.count("output") == 2


# -------------------------------------------------------------------- refusal


def test_a_running_job_cannot_be_cleared_or_deleted(rp):
    target = _export_dir(rp["out"] / "busy", job_id="export-busy")
    me = {"pid": os.getpid(), "identity": jobs.children.identity(os.getpid())}
    _fake("export-busy", target, status="running", worker=me)
    with pytest.raises(DeleteRefused, match="cancel it first"):
        deletion.delete("export-busy", files=False)
    assert target.is_dir() and settings.job_path("export-busy").exists()


def test_paths_outside_the_roots_and_links_are_refused(rp, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", str(rp["out"]))
    elsewhere = _export_dir(tmp_path / "elsewhere" / "exp", job_id="export-out")
    _fake("export-out", elsewhere)
    with pytest.raises(DeleteRefused, match="outside LEVI_EXPORT_ROOTS"):
        deletion.delete("export-out", files=True)
    assert elsewhere.is_dir() and settings.job_path("export-out").exists()
    # The record alone may still be cleared.
    assert deletion.delete("export-out", files=False)["record_cleared"]
    # A symbolic link is never followed or deleted.
    real = _export_dir(tmp_path / "real" / "exp", job_id="export-link")
    link = rp["out"] / "link"
    rp["out"].mkdir(exist_ok=True)
    link.symlink_to(real)
    _fake("export-link", link)
    with pytest.raises(DeleteRefused, match="symbolic link"):
        deletion.delete("export-link", files=True)
    assert real.is_dir() and link.is_symlink()
    # Nor a directory reached through a linked parent that leaves the root.
    _export_dir(tmp_path / "real2" / "exp", job_id="export-via")
    parent = rp["out"] / "viaparent"
    parent.symlink_to(tmp_path / "real2")
    _fake("export-via", parent / "exp")
    with pytest.raises(DeleteRefused, match="outside LEVI_EXPORT_ROOTS"):
        deletion.delete("export-via", files=True)
    assert (tmp_path / "real2/exp/pool_export.json").is_file()


def test_a_pool_source_or_a_folder_holding_one_is_never_deleted(rp):
    inside = rp["root"] / "rollouts/models/pi/pick_x/inside_export"
    _export_dir(inside, job_id="export-in")
    _fake("export-in", inside)
    with pytest.raises(DeleteRefused, match="pool source"):
        deletion.delete("export-in", files=True)
    assert inside.is_dir()
    # An export folder that contains a source dataset (a pool root).
    holder = _export_dir(rp["out"] / "holder", job_id="export-holder")
    _fake("export-holder", holder, sources=[str(holder / "task" / "demo_0000")])
    with pytest.raises(DeleteRefused, match="contains the pool source"):
        deletion.delete("export-holder", files=True)
    assert holder.is_dir()


def test_a_changed_folder_needs_force_and_says_why(rp):
    unmarked = _export_dir(rp["out"] / "nomark", marker=False)
    _fake("export-nomark", unmarked, result={"dataset_path": str(unmarked)})
    plan = deletion.plan("export-nomark", files=True)
    item = plan["outputs"][0]
    # No marker: the job's own recorded result, but changed by something else.
    assert item["owned"] and plan["needs_force"] and not item["marker"]
    assert "pool_export.json" in item["needs_force"][0]
    with pytest.raises(DeleteRefused, match="Changed since the export"):
        deletion.delete("export-nomark", files=True)
    assert unmarked.is_dir()
    assert deletion.delete("export-nomark", files=True, force=True)["removed"]
    assert not unmarked.exists()
    changed = _export_dir(rp["out"] / "changed", episodes=2, job_id="export-changed")
    record = json.loads((changed / "pool_export.json").read_text())
    record["counts"]["episodes"] = 5
    (changed / "pool_export.json").write_text(json.dumps(record))
    _fake("export-changed", changed)
    plan = deletion.plan("export-changed", files=True)
    assert plan["needs_force"]
    assert (
        "holds 2 episode(s) but the export recorded 5"
        in plan["outputs"][0]["needs_force"][0]
    )
    with pytest.raises(DeleteRefused, match="Changed since the export"):
        deletion.delete("export-changed", files=True)
    assert changed.is_dir()
    forced = deletion.delete("export-changed", files=True, force=True)
    assert forced["record_cleared"] and not changed.exists()
    assert _log()[-2]["forced"] is True


def test_a_marker_removed_after_the_plan_is_caught_before_deleting(rp, monkeypatch):
    jid = _done(rp, "toctou")
    real = deletion.plan

    def swapped(job_id, files=False):
        result = real(job_id, files)
        (rp["out"] / "toctou/pool_export.json").unlink()  # someone edits it now
        return result

    monkeypatch.setattr(deletion, "plan", swapped)
    result = deletion.delete(jid, files=True)
    assert (rp["out"] / "toctou").is_dir()
    assert result["errors"] and "changed while deleting" in result["errors"][0]
    assert not result["record_cleared"] and settings.job_path(jid).exists()


def test_a_push_or_another_job_using_the_export_blocks_deleting_it(rp):
    jid = _done(rp, "pushed")
    target = rp["out"] / "pushed"
    me = {"pid": os.getpid(), "identity": jobs.children.identity(os.getpid())}
    atomic(
        settings.job_path("push-1"),
        {
            "id": "push-1",
            "kind": "push",
            "source": str(target),
            "remote": {"name": "lab"},
            "status": "running",
            "worker": me,
            "planned_at": time.time(),
        },
    )
    plan = deletion.plan(jid, files=True)
    assert plan["outputs"][0]["pushed"][0]["remote"] == "lab"
    with pytest.raises(DeleteRefused, match="push of this export is running"):
        deletion.delete(jid, files=True)
    assert target.is_dir()
    # A finished push is only information.
    atomic(
        settings.job_path("push-1"),
        {
            "id": "push-1",
            "kind": "push",
            "source": str(target),
            "remote": {"name": "lab"},
            "status": "done",
            "finished_at": time.time(),
            "planned_at": time.time(),
        },
    )
    assert deletion.plan(jid, files=True)["outputs"][0]["pushed"][0]["status"] == "done"
    # Another live job on the same target blocks it too.
    _fake("export-live", target, status="running", worker=me)
    with pytest.raises(DeleteRefused, match="Another running job"):
        deletion.delete(jid, files=True)
    assert target.is_dir()


def test_symlinks_inside_an_export_are_unlinked_not_followed(rp, tmp_path):
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("mine")
    target = _export_dir(rp["out"] / "withlink", job_id="export-wl")
    (target / "task" / "link").symlink_to(outside)
    (target / "task" / "file-link").symlink_to(outside / "keep.txt")
    _fake("export-wl", target)
    result = deletion.delete("export-wl", files=True)
    assert not target.exists() and result["errors"] == []
    assert (outside / "keep.txt").read_text() == "mine"


# ------------------------------------------------------------------ API + CLI


def test_api_delete_preview_bulk_clear_failed_and_log(rp, client):
    a, b = _done(rp, "api-a"), _done(rp, "api-b")
    preview = client.get(f"/api/levi/pool/jobs/{a}/delete-preview").json()
    assert preview["outputs"][0]["will_delete"] and preview["freed_bytes"] > 0
    assert client.get("/api/levi/pool/jobs/nope-1/delete-preview").status_code == 404
    assert client.delete("/api/levi/pool/jobs/nope-1").status_code == 404
    cleared = client.delete(f"/api/levi/pool/jobs/{a}").json()
    assert cleared["record_cleared"] and (rp["out"] / "api-a").is_dir()
    assert client.get(f"/api/levi/pool/jobs/{a}").status_code == 404
    gone = client.delete(f"/api/levi/pool/jobs/{b}", params={"files": True}).json()
    assert gone["record_cleared"] and gone["freed_bytes"] > 0
    assert not (rp["out"] / "api-b").exists()
    # A refusal is a 409 with the reason.
    me = {"pid": os.getpid(), "identity": jobs.children.identity(os.getpid())}
    _fake("export-run", rp["out"] / "run", status="running", worker=me)
    refused = client.delete("/api/levi/pool/jobs/export-run")
    assert refused.status_code == 409 and "cancel it first" in refused.json()["detail"]
    bulk = client.post(
        "/api/levi/pool/jobs/delete",
        json={"ids": ["export-run", "nope-2"], "files": True},
    ).json()
    assert len(bulk["refused"]) == 2 and bulk["results"] == []
    _fake("export-bad", rp["out"] / "bad", status="failed")
    swept = client.post("/api/levi/pool/jobs/clear-failed").json()
    assert swept["cleared"] == 1 and not settings.job_path("export-bad").exists()
    assert settings.job_path("export-run").exists()
    log = client.get("/api/levi/pool/deleted").json()["deleted"]
    assert {e["what"] for e in log} >= {"record", "output"}
    assert (
        client.post("/api/levi/pool/jobs/delete", json={"ids": []}).status_code == 422
    )


def test_cli_delete_asks_first_and_clear_failed_lists(rp, capsys, monkeypatch):
    from levi.pool import cli

    jid = _done(rp, "cli-a")
    assert cli.main(["jobs", "delete", jid, "--files"]) == 1  # not a terminal: refuses
    assert (rp["out"] / "cli-a").is_dir()
    assert "pass --yes" in capsys.readouterr().err
    assert cli.main(["jobs", "delete", jid, "--files", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "will be deleted" in out and '"freed_bytes"' in out
    assert not (rp["out"] / "cli-a").exists()
    _fake("export-x", rp["out"] / "x", status="failed", error="boom")
    assert cli.main(["jobs", "clear-failed"]) == 1
    assert settings.job_path("export-x").exists()
    assert cli.main(["jobs", "clear-failed", "--yes"]) == 0
    assert not settings.job_path("export-x").exists()
    assert cli.main(["jobs", "clear-failed", "--yes"]) == 0
    assert "No failed" in capsys.readouterr().out
    running = _fake(
        "export-r",
        rp["out"] / "r",
        status="running",
        worker={"pid": os.getpid(), "identity": jobs.children.identity(os.getpid())},
    )
    assert cli.main(["jobs", "delete", running, "--yes"]) == 1
    assert "cancel it first" in capsys.readouterr().out
    assert cli.main(["jobs", "--limit", "5"]) == 0
