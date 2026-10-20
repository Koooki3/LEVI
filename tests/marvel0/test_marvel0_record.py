"""``levi/marvel0/record.py``: the policy-server deploy's files under the product
workspace: the record, the idempotency files of the requests, the unit logs and
the audit lines. Every folder here is a temporary one."""

import json
import os
import re
import stat
import threading
import time

import pytest

from levi import own_units
from levi.marvel0 import record
from levi.workspace import Workspace

NOW = 1_792_000_000.0  # 2026-10-14 in UTC; a fixed clock


@pytest.fixture
def workspace(tmp_path):
    return Workspace.at(tmp_path / "ws")


@pytest.fixture
def folder(workspace):
    return record.state_dir((), workspace)


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


# --------------------------------------------------------------- the state folder


def test_the_state_folder_is_private_and_has_its_subfolders(workspace):
    folder = record.state_dir((), workspace)
    assert folder == workspace.state / "marvel0" / "deploy"
    for sub in (folder, folder / "logs", folder / "requests"):
        assert sub.is_dir() and mode(sub) == 0o700


def test_asking_without_creating_creates_nothing(workspace):
    folder = record.state_dir((), workspace, create=False)
    assert folder == workspace.state / "marvel0" / "deploy"
    assert not (workspace.state / "marvel0").exists()


def test_the_default_workspace_is_the_one_in_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVI_WORKSPACE", str(tmp_path / "from-env"))
    folder = record.state_dir((), create=False)
    assert folder == Workspace.at(tmp_path / "from-env").state / "marvel0" / "deploy"


def test_a_symlinked_folder_is_refused(workspace, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    workspace.state.mkdir(parents=True)
    (workspace.state / "marvel0").symlink_to(elsewhere)
    with pytest.raises(record.StateDirUnusable):
        record.state_dir((), workspace)
    assert list(elsewhere.iterdir()) == []


def test_a_symlinked_subfolder_is_refused(workspace, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    folder = record.state_dir((), workspace)
    (folder / "logs").rmdir()
    (folder / "logs").symlink_to(elsewhere)
    with pytest.raises(record.StateDirUnusable):
        record.state_dir((), workspace)


def test_a_path_with_unsafe_characters_is_refused(tmp_path):
    with pytest.raises(record.StateDirUnusable):
        record.state_dir((), Workspace.at(tmp_path / "with space"))
    with pytest.raises(record.StateDirUnusable):
        record.state_dir((), Workspace.at(tmp_path / "pct%"))


def test_a_folder_inside_source_data_is_refused(tmp_path):
    rollouts = tmp_path / "rollouts"
    inside = Workspace.at(rollouts / "ws")
    with pytest.raises(record.StateDirUnusable):
        record.state_dir((rollouts,), inside)
    assert not rollouts.exists(), "nothing is written into source data"
    outside = Workspace.at(tmp_path / "ws")
    assert record.state_dir((rollouts,), outside).is_dir()


def test_a_folder_that_is_a_file_is_refused(workspace):
    workspace.state.mkdir(parents=True)
    (workspace.state / "marvel0").write_text("a file")
    with pytest.raises(record.StateDirUnusable):
        record.state_dir((), workspace)


# --------------------------------------------------------------- the unit names


@pytest.mark.parametrize(
    "model_id, name",
    [
        (
            "pi05_fr3_all_step49999",
            "levi-aeri-policy-manual-marvel0-pi05-fr3-all-step49999",
        ),
        (
            "recap_cfg_r2_best_step14300_jax",
            "levi-aeri-policy-manual-marvel0-recap-cfg-r2-best-step14300-jax",
        ),
        (
            "recap_cfg_r2_step15000_jax",
            "levi-aeri-policy-manual-marvel0-recap-cfg-r2-step15000-jax",
        ),
    ],
)
def test_the_unit_names_of_the_real_checkpoints(model_id, name):
    assert record.unit_name(model_id) == name


@pytest.mark.parametrize(
    "model_id",
    [
        "pi05_fr3_all_step49999",
        "a" * 40,
        "recap_cfg_r2_best_step14300_jax_and_something_longer",
        "Upper_Case",
        "dots.and.more",
        "-leading-dash",
        "x",
    ],
)
def test_every_unit_name_is_one_of_our_policy_units(model_id):
    name = record.unit_name(model_id)
    assert own_units.POLICY_UNIT.fullmatch(name)
    assert record.UNIT_RE.fullmatch(name)
    assert name == record.unit_name(model_id), "deterministic"


def test_a_name_that_is_changed_or_cut_carries_a_hash_of_the_id():
    plain = record.unit_name("pi05_fr3_all_step49999")
    assert not re.search(r"-[0-9a-f]{8}$", plain)
    long = record.unit_name("a" * 40)
    assert re.search(r"-[0-9a-f]{8}$", long)
    assert record.unit_name("a" * 41) != long, "the whole id is hashed"
    assert record.unit_name("Upper_Case") != record.unit_name("upper_case")
    assert len(long) - len(record.UNIT_PREFIX) <= 32


# --------------------------------------------------------------- the record


def test_a_record_round_trips_with_private_permissions(folder):
    assert record.read_record(folder) is None
    record.write_record(folder, {"phase": "intent", "n": 1})
    assert record.read_record(folder) == {"phase": "intent", "n": 1}
    assert mode(folder / "record.json") == 0o600
    record.write_record(folder, {"phase": "started"})
    assert record.read_record(folder) == {"phase": "started"}


def test_a_failed_replace_keeps_the_old_record_and_leaves_no_temporary(
    folder, monkeypatch
):
    record.write_record(folder, {"phase": "intent"})

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        record.write_record(folder, {"phase": "started"})
    monkeypatch.undo()
    assert record.read_record(folder) == {"phase": "intent"}
    assert sorted(p.name for p in folder.iterdir()) == [
        "logs",
        "record.json",
        "requests",
    ]


def test_a_record_that_is_a_symlink_is_not_written_through(folder, tmp_path):
    victim = tmp_path / "victim.json"
    victim.write_text("{}")
    (folder / "record.json").symlink_to(victim)
    with pytest.raises(OSError):
        record.write_record(folder, {"phase": "started"})
    assert victim.read_text() == "{}"
    with pytest.raises(record.RecordCorrupt):
        record.read_record(folder)


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", "", "null"])
def test_a_corrupt_record_is_reported(folder, text):
    (folder / "record.json").write_text(text)
    with pytest.raises(record.RecordCorrupt):
        record.read_record(folder)


def test_a_record_that_is_a_fifo_is_reported_without_blocking(folder):
    os.mkfifo(folder / "record.json")
    with pytest.raises(record.RecordCorrupt):
        record.read_record(folder)


def test_updates_are_serialised_so_writers_lose_nothing(folder):
    record.write_record(folder, {"count": 0})

    def bump():
        for _ in range(60):
            with record.locked(folder):
                value = record.read_record(folder)
                record.write_record(folder, {**value, "count": value["count"] + 1})

    def note():
        for n in range(60):
            record.update_record(folder, last=n)

    threads = [threading.Thread(target=bump) for _ in range(2)]
    threads.append(threading.Thread(target=note))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert record.read_record(folder) == {"count": 120, "last": 59}


def test_update_merges_fields_into_the_record(folder):
    record.write_record(folder, {"phase": "intent", "keep": 1})
    merged = record.update_record(folder, phase="started", extra=2)
    assert merged == {"phase": "started", "keep": 1, "extra": 2}
    assert record.read_record(folder) == merged


# --------------------------------------------------------------- the requests


def test_a_request_is_stored_once_and_read_back(folder):
    rid = "req-0001-abcd"
    assert record.get_request(folder, rid) is None
    record.put_request(folder, rid, {"status": "accepted", "fingerprint": "f"})
    assert record.get_request(folder, rid) == {"status": "accepted", "fingerprint": "f"}
    assert mode(folder / "requests" / f"{rid}.json") == 0o600
    with pytest.raises(FileExistsError):
        record.put_request(folder, rid, {"status": "accepted"})
    record.put_request(folder, rid, {"status": "done"}, replace=True)
    assert record.get_request(folder, rid) == {"status": "done"}


def test_a_request_can_be_forgotten(folder):
    rid = "req-0002-abcd"
    record.put_request(folder, rid, {"status": "accepted"})
    record.drop_request(folder, rid)
    assert record.get_request(folder, rid) is None
    record.drop_request(folder, rid)  # already gone: fine


@pytest.mark.parametrize(
    "rid", ["short", "a" * 81, "has space 123", "../../etc/pass", "with/slash123", ""]
)
def test_a_request_id_that_is_not_a_plain_token_is_refused(folder, rid):
    assert not record.REQUEST_ID.fullmatch(rid)
    with pytest.raises(ValueError):
        record.put_request(folder, rid, {})
    with pytest.raises(ValueError):
        record.get_request(folder, rid)


def test_a_corrupt_request_file_is_reported(folder):
    (folder / "requests" / "req-0003-abcd.json").write_text("{broken")
    with pytest.raises(record.RecordCorrupt):
        record.get_request(folder, "req-0003-abcd")


def test_the_fingerprint_ignores_the_confirmation_and_the_id():
    body = {"request_id": "req-0004-abcd", "model_id": "m", "rtc": True, "confirm": "x"}
    same = {**body, "request_id": "req-0005-abcd", "confirm": "another phrase"}
    base = record.fingerprint("deploy", body)
    assert re.fullmatch(r"[0-9a-f]{64}", base)
    assert record.fingerprint("deploy", same) == base
    assert record.fingerprint("stop", body) != base
    for change in ({"model_id": "n"}, {"rtc": False}, {"fraction": 0.3}):
        assert record.fingerprint("deploy", {**body, **change}) != base


# --------------------------------------------------------------- the logs


def test_a_log_is_created_exclusively_with_private_permissions(folder):
    unit = record.unit_name("pi05_fr3_all_step49999")
    path = record.create_log(folder, unit, NOW)
    assert path.parent == folder / "logs"
    assert re.fullmatch(rf"{unit}-\d{{8}}T\d{{6}}Z\.log", path.name)
    assert path.read_text() == "" and mode(path) == 0o600
    again = record.create_log(folder, unit, NOW)  # the same second: another name
    assert again != path and again.name == path.name.replace(".log", "-2.log")
    assert record.LOG_RE.fullmatch(again.name)
    assert record.log_path(folder, unit, NOW).name.endswith("-3.log")


def test_a_log_name_with_unsafe_characters_is_refused(folder):
    with pytest.raises(ValueError):
        record.create_log(folder, "bad unit", NOW)


def test_old_logs_and_requests_are_pruned_but_only_ours(folder):
    unit = record.unit_name("pi05_fr3_all_step49999")
    logs = folder / "logs"
    names = []
    for n in range(14):
        path = record.create_log(folder, unit, NOW + n)
        os.utime(path, (NOW + n, NOW + n))
        names.append(path.name)
    (logs / "notes.txt").write_text("not ours")
    (logs / f"{unit}-20200101T000000Z.log.bak").write_text("not ours either")
    outside = folder / "outside.log"
    outside.write_text("target")
    (logs / f"{unit}-20190101T000000Z.log").symlink_to(outside)
    for n in range(210):
        rid = f"req-{n:06d}-abcd"
        record.put_request(folder, rid, {"n": n})
        os.utime(folder / "requests" / f"{rid}.json", (NOW + n, NOW + n))
    (folder / "requests" / "keep.me").write_text("not ours")

    record.prune(folder, keep={names[0]})

    left = sorted(p.name for p in logs.iterdir())
    assert names[0] in left, "the log in use is kept whatever its age"
    assert [n for n in names if n in left] == [names[0], *names[-10:]]
    assert "notes.txt" in left
    assert f"{unit}-20200101T000000Z.log.bak" in left
    assert f"{unit}-20190101T000000Z.log" in left, "a symlink is left alone"
    assert outside.read_text() == "target"
    requests = sorted(p.name for p in (folder / "requests").iterdir())
    assert "keep.me" in requests
    assert len([n for n in requests if n.endswith(".json")]) == record.KEEP_REQUESTS
    assert "req-000209-abcd.json" in requests and "req-000000-abcd.json" not in requests


# --------------------------------------------------------------- the audit


def read_history(folder):
    return [
        json.loads(line) for line in (folder / "history.jsonl").read_text().splitlines()
    ]


def test_an_audit_line_names_the_person_and_hides_paths(folder, tmp_path):
    record.audit(
        folder,
        {
            "action": "deploy",
            "phase": "refused",
            "request_id": "req-0006-abcd",
            "model_id": "pi05_fr3_all_step49999",
            "code": "port_taken",
            "detail": f"see {tmp_path}/x/y.log and /home/someone/file " + "z" * 400,
            "token": "secret",
        },
        now=NOW,
    )
    (line,) = read_history(folder)
    assert line["actor"] == "person" and line["via"] == "product"
    assert line["action"] == "deploy" and line["code"] == "port_taken"
    assert "token" not in line
    assert str(tmp_path) not in json.dumps(line) and "/home/" not in json.dumps(line)
    assert len(line["detail"]) <= 300
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:]+[+-]\d\d:\d\d", line["time"])


def test_the_audit_rotates(folder, monkeypatch):
    monkeypatch.setattr(record, "AUDIT_MAX_BYTES", 2000)
    for n in range(40):
        record.audit(
            folder, {"action": "deploy", "detail": "x" * 250, "request_id": str(n)}
        )
    files = sorted(p.name for p in folder.glob("history.jsonl*"))
    assert files == ["history.jsonl", "history.jsonl.1"], "one rotated file is kept"
    assert all(p.stat().st_size <= 2000 + 600 for p in folder.glob("history.jsonl*"))


def test_the_audit_never_raises(folder, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(record.jsonio, "append_line", broken)
    record.audit(folder, {"action": "deploy"})  # no exception


def test_scrub_replaces_absolute_paths_only():
    text = "norm stats at /opt/x/ckpt/norm_stats.json on http://127.0.0.1:8000/ ok"
    assert record.scrub(text) == "norm stats at <path> on http://127.0.0.1:8000/ ok"
    assert record.scrub("scripts/serve_policy_rtc.py") == "scripts/serve_policy_rtc.py"


def test_the_iso_time_has_an_offset():
    stamp = record.iso(NOW)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", stamp)
    assert time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")) == pytest.approx(
        NOW, abs=1
    )
