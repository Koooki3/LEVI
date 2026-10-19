"""Rig status parts (``levi/rig/status.py``): the arm-controller marker, the
demo client's evaluation sessions, our own ``levi-aeri-*`` units and the
advisory ``robot_idle`` verdict that merges them with the setup probes' quiet
guard. Every input is injected (paths, the clock, a fake ``systemctl``, a
fake ``/proc`` under the real quiet guard), so nothing here looks at the real
machine, a process or a port."""

import builtins
import errno
import json
import os
import subprocess
import time
from datetime import UTC, datetime

import pytest
from test_setup_probes import FakeProc, probe

from levi.rig import status as S
from levi.setup import probes

HOUR = 3600.0
MARKER = ".arm-controller-active"


def age_file(path, age, now):
    os.utime(path, (now - age, now - age), follow_symlinks=False)


# ------------------------------------------------------------------ the schema


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# code -> (source, state, params every such reason carries, params it may carry).
# The codes and their params are the contract of ``robot_idle.reasons`` that
# the page translates; ``text`` stays the English detail.
REASON_CODES = {
    "marker.present": ("marker", "busy", {"age_s"}, set()),
    "marker.old": ("marker", "unknown", {"age_s"}, set()),
    "marker.unreadable": ("marker", "unknown", set(), set()),
    "marker.no_folder": ("marker", "unknown", set(), set()),
    "sessions.recent": ("eval_sessions", "busy", {"group", "age_s"}, {"status"}),
    "sessions.silent": ("eval_sessions", "unknown", {"group", "age_s"}, {"status"}),
    "sessions.unreadable": ("eval_sessions", "unknown", set(), set()),
    "units.active": ("units", "busy", {"name", "active", "sub"}, set()),
    "units.state_unknown": ("units", "unknown", {"name", "active"}, set()),
    "units.unreadable": ("units", "unknown", set(), set()),
    "guard.robot_process": ("guard", "busy", {"names"}, set()),
    "guard.controller_active": ("guard", "busy", set(), set()),
    "guard.unreadable": ("guard", "unknown", set(), set()),
    "guard.not_quiet": ("guard", "unknown", set(), set()),
    "guard.old_sample": ("guard", "unknown", set(), set()),
    "guard.truncated": ("guard", "unknown", set(), set()),
    "guard.controller_value": ("guard", "unknown", set(), set()),
}


def assert_reason(reason):
    """One entry of ``robot_idle.reasons``: the three fields the page always
    had, a stable ``code`` from the list above and flat ``params`` (strings and
    numbers, no path)."""
    assert list(reason) == ["source", "state", "text", "code", "params"], reason
    assert all(isinstance(reason[k], str) and reason[k] for k in list(reason)[:4])
    assert reason["code"] in REASON_CODES, reason
    source, state, required, optional = REASON_CODES[reason["code"]]
    assert (reason["source"], reason["state"]) == (source, state), reason
    params = reason["params"]
    assert isinstance(params, dict)
    assert required <= set(params) <= required | optional, params
    for value in params.values():
        assert isinstance(value, str) or is_number(value), params
    json.dumps(reason, allow_nan=False)


def assert_schema(doc):
    """The shared contract of ``GET /api/levi/rig/status`` (``levi.rig.status.v1``)."""
    assert list(doc) == [
        "schema",
        "sampled_at",
        "marker",
        "eval_sessions",
        "units",
        "robot_idle",
    ]
    assert doc["schema"] == "levi.rig.status.v1"
    assert datetime.fromisoformat(doc["sampled_at"]).utcoffset() is not None
    marker = doc["marker"]
    assert set(marker) == {"state", "age_s", "detail"}
    assert marker["state"] in ("absent", "present", "unknown")
    assert marker["age_s"] is None or is_number(marker["age_s"])
    assert marker["detail"] is None or isinstance(marker["detail"], str)
    ev = doc["eval_sessions"]
    assert set(ev) == {"state", "roots", "sessions", "detail"}
    assert ev["state"] in ("ok", "unknown")
    assert isinstance(ev["roots"], int) and not isinstance(ev["roots"], bool)
    assert ev["detail"] is None or isinstance(ev["detail"], str)
    for row in ev["sessions"]:
        assert set(row) == {"group", "age_s", "status"}
        assert isinstance(row["group"], str) and is_number(row["age_s"])
        assert row["status"] is None or isinstance(row["status"], str)
    units = doc["units"]
    assert set(units) == {"state", "items", "detail"}
    assert units["state"] in ("ok", "unknown")
    assert units["detail"] is None or isinstance(units["detail"], str)
    for row in units["items"]:
        assert set(row) == {"name", "active", "sub"}
        assert all(isinstance(v, str) for v in row.values())
    idle = doc["robot_idle"]
    assert set(idle) == {"state", "reasons"}
    assert idle["state"] in ("idle", "busy", "unknown")
    for reason in idle["reasons"]:
        assert_reason(reason)


# ------------------------------------------------------------------ the marker


def test_marker_absent(tmp_path):
    assert S.marker_part(tmp_path / MARKER, time.time()) == {
        "state": "absent",
        "age_s": None,
        "detail": None,
    }


def test_a_missing_marker_folder_is_unknown_not_absent(tmp_path):
    # Nothing can be said about a file whose folder is not there (a wrong
    # checkout, an unmounted disk): that must not read as "no marker".
    now = time.time()
    out = S.marker_part(tmp_path / "no" / "hub" / MARKER, now)
    assert out == {
        "state": "unknown",
        "age_s": None,
        "detail": "the marker folder does not exist",
    }
    (tmp_path / "no").mkdir()
    assert S.marker_part(tmp_path / "no" / "hub" / MARKER, now)["state"] == "unknown"
    (tmp_path / "no" / "hub").mkdir()
    assert S.marker_part(tmp_path / "no" / "hub" / MARKER, now)["state"] == "absent"


def test_a_marker_folder_that_is_a_dangling_link_is_unknown_not_absent(tmp_path):
    (tmp_path / "hub").symlink_to(tmp_path / "gone")
    out = S.marker_part(tmp_path / "hub" / MARKER, time.time())
    assert out["state"] == "unknown" and "folder does not exist" in out["detail"]


def test_a_marker_folder_that_cannot_be_probed_is_unknown(tmp_path, monkeypatch):
    path = tmp_path / "hub" / MARKER
    real_lstat, real_stat = os.lstat, os.stat

    def lstat(target, *args, **kwargs):
        if os.fspath(target) == str(path):
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real_lstat(target, *args, **kwargs)

    def stat(target, *args, **kwargs):
        if os.fspath(target) == str(path.parent):
            raise OSError(errno.ESTALE, "stale")
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os, "stat", stat)
    out = S.marker_part(path, time.time())
    assert out["state"] == "unknown" and "could not be examined" in out["detail"]


def test_marker_present_reports_its_age_and_is_never_cleared(tmp_path):
    now = time.time()
    path = tmp_path / MARKER
    path.write_text("")
    age_file(path, 90, now)
    out = S.marker_part(path, now)
    assert out["state"] == "present" and out["detail"] is None
    assert out["age_s"] == pytest.approx(90.0, abs=0.1)
    assert path.exists()


def test_an_old_marker_is_reported_with_its_age_not_cleared(tmp_path):
    now = time.time()
    path = tmp_path / MARKER
    path.write_text("")
    age_file(path, 30 * HOUR, now)
    out = S.marker_part(path, now)
    assert out["state"] == "present"
    assert out["age_s"] == pytest.approx(30 * HOUR, abs=0.1)
    assert path.exists()


def test_a_marker_from_the_future_has_age_zero(tmp_path):
    now = time.time()
    path = tmp_path / MARKER
    path.write_text("")
    age_file(path, -500, now)
    assert S.marker_part(path, now)["age_s"] == 0.0


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches any folder")
def test_a_marker_that_cannot_be_examined_is_unknown(tmp_path):
    folder = tmp_path / "hub"
    folder.mkdir()
    (folder / MARKER).write_text("")
    folder.chmod(0)
    try:
        out = S.marker_part(folder / MARKER, time.time())
    finally:
        folder.chmod(0o700)
    assert out["state"] == "unknown" and out["age_s"] is None
    assert "could not be examined" in out["detail"]


def test_an_unreadable_regular_marker_is_present_because_it_is_never_opened(tmp_path):
    now = time.time()
    path = tmp_path / MARKER
    path.write_text("")
    age_file(path, 90, now)
    path.chmod(0)
    try:
        out = S.marker_part(path, now)
    finally:
        path.chmod(0o600)
    assert out == {
        "state": "present",
        "age_s": pytest.approx(90.0, abs=0.1),
        "detail": None,
    }


def test_the_marker_is_decided_from_lstat_and_never_opened(tmp_path, monkeypatch):
    now = time.time()
    regular = tmp_path / "regular"
    regular.write_text("")
    link = tmp_path / "link"
    link.symlink_to(regular)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    folder = tmp_path / "folder"
    folder.mkdir()
    opened = []
    real_os_open, real_open = os.open, builtins.open

    def spy_os_open(path, *args, **kwargs):
        opened.append(os.fspath(path))
        return real_os_open(path, *args, **kwargs)

    def spy_open(path, *args, **kwargs):
        opened.append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(builtins, "open", spy_open)
    states = {
        p.name: S.marker_part(p, now)["state"] for p in (regular, link, fifo, folder)
    }
    assert opened == []
    assert states == {
        "regular": "present",
        "link": "unknown",
        "fifo": "unknown",
        "folder": "unknown",
    }


def test_a_symlink_marker_is_refused_and_not_followed(tmp_path):
    real = tmp_path / "real"
    real.write_text("")
    link = tmp_path / MARKER
    link.symlink_to(real)
    out = S.marker_part(link, time.time())
    assert out["state"] == "unknown" and out["age_s"] is None
    assert "not a regular file" in out["detail"]


def test_a_dangling_symlink_marker_is_unknown_not_absent(tmp_path):
    link = tmp_path / MARKER
    link.symlink_to(tmp_path / "gone")
    out = S.marker_part(link, time.time())
    assert out["state"] == "unknown" and "not a regular file" in out["detail"]


def test_a_directory_or_fifo_marker_is_unknown_without_blocking(tmp_path):
    folder = tmp_path / "dir" / MARKER
    folder.mkdir(parents=True)
    assert S.marker_part(folder, time.time())["state"] == "unknown"
    fifo = tmp_path / MARKER
    os.mkfifo(fifo)
    assert S.marker_part(fifo, time.time())["state"] == "unknown"


def test_no_marker_location_is_unknown():
    out = S.marker_part(None, time.time())
    assert out["state"] == "unknown" and out["detail"]


# ------------------------------------------------------------------ sessions


def write_session(root, stem, now, age, *, mtime_age=None, **fields):
    """``<root>/.eval_sessions/<stem>.json`` as the demo client writes it."""
    folder = root / ".eval_sessions"
    folder.mkdir(exist_ok=True)
    body = {"group": "g1", "state": "running", "updated_at": now - age, **fields}
    body = {k: v for k, v in body.items() if v is not None}
    path = folder / f"{stem}.json"
    path.write_text(json.dumps(body))
    age_file(path, age if mtime_age is None else mtime_age, now)
    return path


def test_a_fresh_session_is_listed(tmp_path):
    now = time.time()
    write_session(tmp_path, "g1__plates", now, 5, state="homing")
    out = S.sessions_part([str(tmp_path)], now)
    assert out["state"] == "ok" and out["roots"] == 1 and out["detail"] is None
    assert out["sessions"] == [{"group": "g1", "age_s": 5.0, "status": "homing"}]


def test_sessions_are_read_by_iso_time_and_sorted_freshest_first(tmp_path):
    now = time.time()
    # The client writes naive local time.
    local = datetime.fromtimestamp(now - 100, tz=UTC).astimezone()
    stamp = local.replace(tzinfo=None).isoformat()
    write_session(tmp_path, "a__x", now, 100, group="a", updated_at=stamp)
    write_session(tmp_path, "b__x", now, 20, group="b")
    out = S.sessions_part([str(tmp_path)], now)
    assert [r["group"] for r in out["sessions"]] == ["b", "a"]
    assert out["sessions"][1]["age_s"] == pytest.approx(100.0, abs=0.5)


@pytest.mark.parametrize("ended", ["fault", "stopped", "finished"])
def test_an_ended_session_is_listed_for_ten_minutes(tmp_path, ended):
    now = time.time()
    write_session(tmp_path, "g1__fresh", now, 599, group="fresh", state=ended)
    write_session(tmp_path, "g1__old", now, 601, group="old", state=ended)
    out = S.sessions_part([str(tmp_path)], now)
    assert out["state"] == "ok"
    assert [r["group"] for r in out["sessions"]] == ["fresh"]


def test_a_session_that_has_not_ended_is_listed_for_an_hour(tmp_path):
    # eval_session.py: a file not in fault/stopped/finished that stays silent
    # is "crashed" only after an hour (or when its pid is gone, which this
    # module never asks); until then the robot cannot be ruled out.
    now = time.time()
    write_session(tmp_path, "g1__a", now, 3000, group="a", state="running")
    write_session(tmp_path, "g1__b", now, 3000, group="b", state="finished")
    write_session(tmp_path, "g1__c", now, 3599, group="c", state="homing")
    write_session(tmp_path, "g1__d", now, 3601, group="d", state="waiting_reset")
    write_session(tmp_path, "g1__e", now, 3100, group="e", state=None)
    out = S.sessions_part([str(tmp_path)], now)
    assert out["state"] == "ok" and out["detail"] is None
    assert [r["group"] for r in out["sessions"]] == ["a", "e", "c"]


def test_the_ended_states_are_the_clients_own(tmp_path):
    assert S.ENDED_STATES == ("fault", "stopped", "finished")


def test_the_group_falls_back_to_the_file_name_and_time_to_the_file(tmp_path):
    now = time.time()
    path = write_session(tmp_path, "grp7__task", now, 30, group=None, updated_at=None)
    out = S.sessions_part([str(tmp_path)], now)
    assert out["sessions"][0]["group"] == "grp7"
    assert out["sessions"][0]["age_s"] == pytest.approx(30.0, abs=0.5)
    assert path.exists()


def test_a_session_without_a_state_has_status_none(tmp_path):
    now = time.time()
    write_session(tmp_path, "g1__t", now, 3, state=None)
    assert S.sessions_part([str(tmp_path)], now)["sessions"][0]["status"] is None


def test_non_finite_times_never_reach_the_json(tmp_path):
    now = time.time()
    folder = tmp_path / ".eval_sessions"
    folder.mkdir()
    (folder / "g__t.json").write_text('{"group": "g", "updated_at": NaN}')
    out = S.sessions_part([str(tmp_path)], now)
    json.dumps(out, allow_nan=False)


def test_a_lone_surrogate_in_a_session_never_breaks_the_json(tmp_path):
    now = time.time()
    folder = tmp_path / ".eval_sessions"
    folder.mkdir()
    (folder / "g__t.json").write_text(
        json.dumps({"group": "g\ud800x", "state": "run\udfffning", "updated_at": now})
    )
    out = S.sessions_part([str(tmp_path)], now)
    assert len(out["sessions"]) == 1
    json.dumps(out, ensure_ascii=False).encode("utf-8")


def test_a_deeply_nested_session_file_is_unusable_not_fatal(tmp_path):
    now = time.time()
    folder = tmp_path / ".eval_sessions"
    folder.mkdir()
    (folder / "g__t.json").write_text("[" * 100000)
    out = S.sessions_part([str(tmp_path)], now)
    assert out["state"] == "unknown" and "1 session file(s)" in out["detail"]


def test_non_regular_session_files_are_never_opened(tmp_path, monkeypatch):
    now = time.time()
    folder = tmp_path / ".eval_sessions"
    folder.mkdir()
    real = write_session(tmp_path, "g1__real", now, 10, group="real")
    link = folder / "g1__link.json"
    link.symlink_to(real)
    age_file(link, 10, now)
    fifo = folder / "g1__fifo.json"
    os.mkfifo(fifo)
    age_file(fifo, 10, now)
    opened = []
    real_open = os.open

    def spy(path, *args, **kwargs):
        opened.append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    out = S.sessions_part([str(tmp_path)], now)
    assert opened == [str(real)]
    # The regular file is listed; the link and the FIFO are not, and an
    # unusable recent file means the part cannot claim to have seen everything.
    assert [r["group"] for r in out["sessions"]] == ["real"]
    assert out["state"] == "unknown"
    assert "2 session file(s)" in out["detail"]


def test_old_non_regular_entries_are_ignored_quietly(tmp_path):
    now = time.time()
    folder = tmp_path / ".eval_sessions"
    folder.mkdir()
    fifo = folder / "g1__fifo.json"
    os.mkfifo(fifo)
    age_file(fifo, 5 * HOUR, now)
    out = S.sessions_part([str(tmp_path)], now)
    assert out["state"] == "ok" and out["sessions"] == []


def test_corrupt_json_is_skipped_and_makes_the_part_unknown(tmp_path):
    now = time.time()
    folder = tmp_path / ".eval_sessions"
    folder.mkdir()
    (folder / "g__half.json").write_text('{"group": "g", "updated_at"')
    (folder / "g__list.json").write_text("[1, 2]")
    write_session(tmp_path, "g__ok", now, 4, group="ok")
    out = S.sessions_part([str(tmp_path)], now)
    assert [r["group"] for r in out["sessions"]] == ["ok"]
    assert out["state"] == "unknown" and "2 session file(s)" in out["detail"]


def test_old_corrupt_json_is_ignored(tmp_path):
    now = time.time()
    folder = tmp_path / ".eval_sessions"
    folder.mkdir()
    path = folder / "g__half.json"
    path.write_text("{")
    age_file(path, 2 * HOUR, now)
    out = S.sessions_part([str(tmp_path)], now)
    assert out["state"] == "ok" and out["sessions"] == []


def test_corrupt_json_younger_than_the_longest_window_still_counts(tmp_path):
    # A file that has not ended can be listed for an hour, so a file that
    # young that cannot be read might have been one.
    now = time.time()
    folder = tmp_path / ".eval_sessions"
    folder.mkdir()
    path = folder / "g__half.json"
    path.write_text("{")
    age_file(path, 3000, now)
    out = S.sessions_part([str(tmp_path)], now)
    assert out["state"] == "unknown" and "1 session file(s)" in out["detail"]


def test_an_oversized_session_file_is_not_read_whole(tmp_path):
    now = time.time()
    folder = tmp_path / ".eval_sessions"
    folder.mkdir()
    path = folder / "g__big.json"
    path.write_text('{"group": "g", "pad": "' + "x" * (S.MAX_SESSION_BYTES + 10) + '"}')
    out = S.sessions_part([str(tmp_path)], now)
    assert out["sessions"] == [] and out["state"] == "unknown"


def test_only_json_files_that_are_not_hidden_count(tmp_path):
    now = time.time()
    folder = tmp_path / ".eval_sessions"
    folder.mkdir()
    for name in ("g__t.json.tmp", ".g__t.json", "notes.txt"):
        (folder / name).write_text('{"group": "g"}')
    out = S.sessions_part([str(tmp_path)], now)
    assert out["state"] == "ok" and out["sessions"] == []


def test_a_root_without_a_session_folder_is_fine(tmp_path):
    (tmp_path / "other").mkdir()
    out = S.sessions_part([str(tmp_path), str(tmp_path / "other")], time.time())
    assert out == {"state": "ok", "roots": 2, "sessions": [], "detail": None}


def test_a_root_that_does_not_exist_is_unknown_not_empty(tmp_path):
    # An unmounted disk looks like a root with no sessions; it must not read as idle.
    out = S.sessions_part([str(tmp_path / "missing")], time.time())
    assert out["state"] == "unknown" and "1 of 1 rollout root(s)" in out["detail"]
    assert out["sessions"] == []


def test_sessions_of_several_roots_are_merged(tmp_path):
    now = time.time()
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    write_session(one, "a__t", now, 50, group="a")
    write_session(two, "b__t", now, 10, group="b")
    out = S.sessions_part([str(one), str(two)], now)
    assert out["roots"] == 2 and [r["group"] for r in out["sessions"]] == ["b", "a"]


def test_an_unreadable_root_is_unknown_but_keeps_what_was_found(tmp_path, monkeypatch):
    now = time.time()
    good, bad = tmp_path / "good", tmp_path / "bad"
    good.mkdir()
    (bad / ".eval_sessions").mkdir(parents=True)
    write_session(good, "g__t", now, 5, group="g")
    real = os.scandir

    def scandir(path):
        if os.fspath(path).startswith(str(bad)):
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    out = S.sessions_part([str(good), str(bad)], now)
    assert out["state"] == "unknown" and "1 of 2 rollout root(s)" in out["detail"]
    assert [r["group"] for r in out["sessions"]] == ["g"]


def test_a_root_whose_existence_probe_fails_too_does_not_stop_the_others(
    tmp_path, monkeypatch
):
    now = time.time()
    good, bad = tmp_path / "good", tmp_path / "bad"
    good.mkdir()
    bad.mkdir()
    write_session(good, "g__t", now, 5, group="g")
    real_scandir, real_stat = os.scandir, os.stat

    def scandir(path, *args, **kwargs):
        if os.fspath(path).startswith(str(bad)):
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real_scandir(path, *args, **kwargs)

    def stat(path, *args, **kwargs):
        if not isinstance(path, int) and os.fspath(path).startswith(str(bad)):
            raise OSError(errno.ESTALE, "stale file handle")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "stat", stat)
    # The failing root comes first: the loop must go on to the healthy one.
    out = S.sessions_part([str(bad), str(good)], now)
    assert [r["group"] for r in out["sessions"]] == ["g"]
    assert out["state"] == "unknown" and "1 of 2 rollout root(s)" in out["detail"]


def test_no_roots_is_unknown_with_the_reason_given(tmp_path):
    out = S.sessions_part([], time.time(), "no live workspace is known (not_live)")
    assert out["state"] == "unknown" and out["roots"] == 0 and out["sessions"] == []
    assert out["detail"] == "no live workspace is known (not_live)"
    assert S.sessions_part([], time.time())["detail"]


def test_a_flood_of_recent_sessions_lists_the_freshest(tmp_path):
    now = time.time()
    for n in range(S.MAX_SESSIONS + 5):
        write_session(tmp_path, f"g{n:03d}__t", now, 10 + n, group=f"g{n:03d}")
    out = S.sessions_part([str(tmp_path)], now)
    assert len(out["sessions"]) == S.MAX_SESSIONS
    assert out["sessions"][0]["group"] == "g000"
    assert out["state"] == "ok" and "most recent" in out["detail"]


def test_a_scan_cut_short_by_the_file_cap_is_unknown(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(S, "MAX_SESSION_FILES", 3)
    for n in range(5):
        write_session(tmp_path, f"g{n}__t", now, 10 + n, group=f"g{n}")
    out = S.sessions_part([str(tmp_path)], now)
    assert out["state"] == "unknown" and "stopped after 3 files" in out["detail"]


# ------------------------------------------------------------------ units

# Lines as ``systemctl --user list-units --type=service,scope --all
# --no-legend --plain "levi-aeri-*"`` prints them: UNIT LOAD ACTIVE SUB
# DESCRIPTION. The scope is a policy server started by hand.
REAL_LINES = """\
levi-aeri-policy-pi05.service  loaded active   running LEVI AERI policy server pi05
levi-aeri-20261018-1530.service loaded activating start   start  LEVI AERI run 20261018-1530
levi-aeri-policy-manual-20261018-1500.scope loaded active running /usr/bin/bash
levi-aeri-vlm-qwen38.service   loaded failed   failed  LEVI AERI vision model
levi-aeri-old.service          not-found inactive dead    levi-aeri-old.service
"""


def test_parse_real_list_units_lines():
    assert S.parse_units(REAL_LINES) == [
        {
            "name": "levi-aeri-20261018-1530.service",
            "active": "activating",
            "sub": "start",
        },
        {
            "name": "levi-aeri-policy-manual-20261018-1500.scope",
            "active": "active",
            "sub": "running",
        },
        {"name": "levi-aeri-policy-pi05.service", "active": "active", "sub": "running"},
        {"name": "levi-aeri-old.service", "active": "inactive", "sub": "dead"},
        {"name": "levi-aeri-vlm-qwen38.service", "active": "failed", "sub": "failed"},
    ]


def test_parse_units_ignores_noise_and_other_units():
    text = (
        "\n"
        "   \n"
        "● levi-aeri-run-1.service loaded failed failed LEVI run 1\n"
        "levi-product.service loaded active running LEVI\n"
        "heavy-gate-x.scope loaded active running gate\n"
        "levi-aeri-short.service loaded\n"
        "Failed to connect to bus: No medium found\n"
        "levi-aeri-$(rm).service loaded active running evil\n"
    )
    assert S.parse_units(text) == [
        {"name": "levi-aeri-run-1.service", "active": "failed", "sub": "failed"}
    ]


def runner_of(code, out):
    seen = []

    def run(argv, timeout):
        seen.append((list(argv), timeout))
        return code, out

    run.seen = seen
    return run


def test_units_part_asks_only_for_our_own_units_with_a_short_timeout():
    run = runner_of(0, REAL_LINES)
    out = S.units_part(run)
    assert run.seen == [
        (
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=service,scope",
                "--all",
                "--no-legend",
                "--plain",
                "--full",
                "levi-aeri-*",
            ],
            2.0,
        )
    ]
    assert out["state"] == "ok" and out["detail"] is None
    assert [u["name"] for u in out["items"]][:3] == [
        "levi-aeri-20261018-1530.service",
        "levi-aeri-policy-manual-20261018-1500.scope",
        "levi-aeri-policy-pi05.service",
    ]


@pytest.mark.parametrize(
    "line",
    [
        "levi-aeri-policy-manual-2026101… loaded active running LEVI AERI policy\n",
        "levi-aeri-run-7.service loaded\n",
    ],
)
def test_a_row_of_ours_that_cannot_be_read_makes_the_part_unknown(line):
    good = "levi-aeri-policy-pi05.service loaded active running x\n"
    out = S.units_part(runner_of(0, good + line))
    assert out["state"] == "unknown" and "could not be read" in out["detail"]
    assert [u["name"] for u in out["items"]] == ["levi-aeri-policy-pi05.service"]


def test_a_policy_scope_started_by_hand_makes_the_rig_busy():
    scope = "levi-aeri-policy-manual-20261018-1500.scope loaded active running x\n"
    out = verdict(u=S.units_part(runner_of(0, scope)))
    assert out["state"] == "busy"
    assert [r["source"] for r in out["reasons"]] == ["units"]
    assert "levi-aeri-policy-manual-20261018-1500.scope" in out["reasons"][0]["text"]


def test_no_unit_is_ok_and_empty():
    assert S.units_part(runner_of(0, "")) == {
        "state": "ok",
        "items": [],
        "detail": None,
    }


def test_missing_systemctl_is_unknown():
    def missing(argv, timeout):
        raise FileNotFoundError(2, "No such file", "systemctl")

    out = S.units_part(missing)
    assert out["state"] == "unknown" and out["items"] == []
    assert "not installed" in out["detail"]


def test_a_systemctl_timeout_is_unknown():
    def slow(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    out = S.units_part(slow)
    assert out["state"] == "unknown" and "within 2 s" in out["detail"]


@pytest.mark.parametrize(
    "failure", [OSError("boom"), ValueError("not text"), subprocess.SubprocessError()]
)
def test_other_systemctl_failures_are_unknown(failure):
    def broken(argv, timeout):
        raise failure

    out = S.units_part(broken)
    assert out["state"] == "unknown" and type(failure).__name__ in out["detail"]


def test_a_failing_exit_code_is_unknown_even_with_output():
    out = S.units_part(runner_of(1, REAL_LINES))
    assert out["state"] == "unknown" and out["items"] == []
    assert "exited with 1" in out["detail"]


def test_active_units_come_first_when_the_list_is_capped():
    lines = "".join(
        f"levi-aeri-dead{n:03d}.service loaded inactive dead x\n"
        for n in range(S.MAX_UNITS + 10)
    )
    lines += "levi-aeri-zzz-live.service loaded active running x\n"
    lines += "levi-aeri-zzz-odd.service loaded maintenance dead x\n"
    out = S.units_part(runner_of(0, lines))
    assert len(out["items"]) == S.MAX_UNITS
    # Busy first, then a state nobody here knows, never the quiet ones first.
    assert [u["name"] for u in out["items"][:2]] == [
        "levi-aeri-zzz-live.service",
        "levi-aeri-zzz-odd.service",
    ]
    assert "first" in out["detail"]


def test_the_default_runner_never_uses_a_shell(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="x", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert S.run_systemctl(["systemctl", "--user", "list-units"], 2.0) == (0, "x")
    argv, kwargs = calls[0]
    assert isinstance(argv, list) and not kwargs.get("shell")
    assert kwargs["timeout"] == 2.0 and kwargs["capture_output"] and kwargs["text"]


# ------------------------------------------------------------------ robot_idle


def guard(robot=(), quiet=True, state="ok", controller_active=None, **extra):
    return {
        "state": state,
        "quiet": quiet,
        "robot": [{"pid": 4242, "name": n} for n in robot],
        "controller_active": controller_active,
        **extra,
    }


def marker(state="absent", age=None, detail=None):
    return {"state": state, "age_s": age, "detail": detail}


def sessions(*rows, state="ok", detail=None):
    return {
        "state": state,
        "roots": 1,
        "sessions": [{"group": g, "age_s": a, "status": s} for g, a, s in rows],
        "detail": detail,
    }


def units(*rows, state="ok", detail=None):
    return {
        "state": state,
        "items": [{"name": n, "active": a, "sub": s} for n, a, s in rows],
        "detail": detail,
    }


UNSET = object()


def verdict(g=UNSET, m=UNSET, e=UNSET, u=UNSET):
    """``robot_idle`` over quiet defaults; a part passed as ``None`` stays None.
    Every reason it gives is checked against the code and params contract."""
    out = S.robot_idle(
        guard() if g is UNSET else g,
        marker() if m is UNSET else m,
        sessions() if e is UNSET else e,
        units() if u is UNSET else u,
    )
    for reason in out["reasons"]:
        assert_reason(reason)
    return out


def codes(out):
    return [r["code"] for r in out["reasons"]]


def test_everything_quiet_is_idle():
    assert verdict() == {"state": "idle", "reasons": []}


def test_a_robot_process_in_the_guard_alone_is_busy():
    out = verdict(g=guard(robot=["ros2_control_node", "franka_server"]))
    assert out["state"] == "busy"
    assert [r["source"] for r in out["reasons"]] == ["guard"]
    assert out["reasons"][0]["state"] == "busy"
    assert "ros2_control_node" in out["reasons"][0]["text"]
    assert codes(out) == ["guard.robot_process"]
    assert out["reasons"][0]["params"] == {"names": "franka_server, ros2_control_node"}


def test_a_strained_host_without_a_robot_process_is_unknown_not_idle():
    # quiet False with no process listed: the guard cannot tell "none found"
    # from "could not look" there, so the verdict is not idle.
    out = verdict(g=guard(quiet=False, strained=["cpu pressure some avg10=40 > 25"]))
    assert out["state"] == "unknown"
    assert [(r["source"], r["state"]) for r in out["reasons"]] == [("guard", "unknown")]
    assert "cpu pressure some avg10=40 > 25" in out["reasons"][0]["text"]
    assert codes(out) == ["guard.not_quiet"]


def test_a_robot_process_found_makes_quiet_false_busy_without_an_unknown_reason():
    out = verdict(g=guard(robot=["franka_server"], quiet=False, strained=["swap 60%"]))
    assert out["state"] == "busy"
    assert [(r["source"], r["state"]) for r in out["reasons"]] == [("guard", "busy")]


def test_an_active_controller_alone_is_busy():
    out = verdict(g=guard(controller_active=True))
    assert out["state"] == "busy"
    assert [(r["source"], r["state"]) for r in out["reasons"]] == [("guard", "busy")]
    assert "controller" in out["reasons"][0]["text"]
    assert codes(out) == ["guard.controller_active"]


@pytest.mark.parametrize("flag", [False, None])
def test_a_false_or_missing_controller_flag_adds_nothing(flag):
    assert verdict(g=guard(controller_active=flag)) == {"state": "idle", "reasons": []}


@pytest.mark.parametrize("flag", ["yes", "false", 1, 2.5, [0], {"a": 0}])
def test_a_truthy_controller_flag_that_is_not_a_bool_still_counts_as_active(flag):
    out = verdict(g=guard(controller_active=flag))
    assert out["state"] == "busy" and codes(out) == ["guard.controller_active"]


@pytest.mark.parametrize("flag", [0, 0.0, "", [], {}])
def test_a_falsy_controller_flag_that_is_not_a_bool_cannot_be_read(flag):
    # The health file said something other than true or false: neither "active"
    # nor "not active" can be claimed, so the verdict is not idle.
    out = verdict(g=guard(controller_active=flag))
    assert out["state"] == "unknown" and codes(out) == ["guard.controller_value"]
    assert out["reasons"][0]["params"] == {}


def test_an_active_controller_with_a_strained_host_is_busy_and_explains_both():
    out = verdict(g=guard(controller_active=True, quiet=False, strained=["io"]))
    assert out["state"] == "busy"
    assert [(r["source"], r["state"]) for r in out["reasons"]] == [
        ("guard", "busy"),
        ("guard", "unknown"),
    ]


def test_a_guard_that_did_not_answer_is_unknown_even_if_its_flag_says_quiet():
    out = verdict(g=guard(state="sampling", quiet=True))
    assert out["state"] == "unknown"
    assert [r["source"] for r in out["reasons"]] == ["guard"]
    assert codes(out) == ["guard.unreadable"]


def test_a_cut_robot_scan_has_its_own_code_before_the_strained_host():
    out = verdict(g=guard(quiet=None, truncated=True))
    assert out["state"] == "unknown" and codes(out) == ["guard.truncated"]
    both = verdict(g=guard(quiet=False, truncated=True, strained=["io"]))
    assert codes(both) == ["guard.truncated"]
    # A guard that is not quiet and cut nothing short is just not quiet.
    assert codes(verdict(g=guard(quiet=None, truncated=False))) == ["guard.unreadable"]


def test_an_old_setup_sample_is_told_apart_from_a_guard_that_failed():
    old = {"state": "unknown", "detail": "old", "code": "guard.old_sample"}
    assert codes(verdict(g=old)) == ["guard.old_sample"]
    # Only that one code is taken from the guard dict.
    other = {"state": "unknown", "detail": "x", "code": "guard.truncated"}
    assert codes(verdict(g=other)) == ["guard.unreadable"]


@pytest.mark.parametrize(
    "age, expected, code",
    [
        (0.0, "busy", "marker.present"),
        (HOUR, "busy", "marker.present"),
        (12 * HOUR - 1, "busy", "marker.present"),
        (12 * HOUR - 0.04, "busy", "marker.present"),
        (12 * HOUR, "unknown", "marker.old"),
        (12 * HOUR + 0.04, "unknown", "marker.old"),
        (30 * HOUR, "unknown", "marker.old"),
    ],
)
def test_a_present_marker_is_never_idle_but_only_young_ones_are_busy(
    age, expected, code
):
    out = verdict(m=marker("present", age))
    assert out["state"] == expected
    assert [(r["source"], r["code"]) for r in out["reasons"]] == [("marker", code)]
    assert out["reasons"][0]["params"] == {"age_s": round(age, 1)}


def test_an_old_marker_says_what_it_may_be_and_how_to_remove_it():
    text = verdict(m=marker("present", 30 * HOUR))["reasons"][0]["text"]
    assert "30.0 h" in text
    assert "crash" in text and "very long run" in text
    assert "remove it by hand if the arm stack is down" in text


def test_a_present_marker_without_a_readable_age_is_busy_without_params():
    out = S.robot_idle(guard(), marker("present", None), sessions(), units())
    assert out["state"] == "busy"
    assert [(r["code"], r["params"]) for r in out["reasons"]] == [
        ("marker.present", {})
    ]


def test_the_marker_boundary_is_decided_before_the_age_is_rounded(tmp_path):
    # 12 h - 0.04 s and 12 h + 0.04 s both print as 43200.0 s; only the
    # unrounded age tells them apart.
    now = time.time()
    path = tmp_path / MARKER
    path.write_text("")
    seen = {}
    for label, age in (("young", 12 * HOUR - 0.04), ("old", 12 * HOUR + 0.04)):
        age_file(path, age, now)
        doc = S.document(now, S.marker_part(path, now), sessions(), units(), guard())
        assert doc["marker"]["age_s"] == 43200.0
        seen[label] = codes(doc["robot_idle"])
    assert seen == {"young": ["marker.present"], "old": ["marker.old"]}


def test_a_marker_that_cannot_be_read_has_one_of_two_codes():
    no_folder = marker("unknown", None, "the marker folder does not exist")
    assert codes(verdict(m=no_folder)) == ["marker.no_folder"]
    other = marker("unknown", None, "the marker could not be examined")
    assert codes(verdict(m=other)) == ["marker.unreadable"]
    assert codes(verdict(m=None)) == ["marker.unreadable"]


@pytest.mark.parametrize(
    "age, expected, code",
    [
        (0.0, "busy", "sessions.recent"),
        (59.9, "busy", "sessions.recent"),
        (59.96, "busy", "sessions.recent"),
        (60.0, "busy", "sessions.recent"),
        (60.04, "unknown", "sessions.silent"),
        (60.1, "unknown", "sessions.silent"),
        (500.0, "unknown", "sessions.silent"),
        (3000.0, "unknown", "sessions.silent"),
    ],
)
def test_a_session_that_has_not_ended_is_busy_for_60_s_then_unknown(
    age, expected, code
):
    out = verdict(e=sessions(("g1", age, "running")))
    assert out["state"] == expected
    assert [(r["source"], r["code"]) for r in out["reasons"]] == [
        ("eval_sessions", code)
    ]
    assert "g1" in out["reasons"][0]["text"]
    assert out["reasons"][0]["params"] == {
        "group": "g1",
        "age_s": round(age, 1),
        "status": "running",
    }


@pytest.mark.parametrize("ended", ["fault", "stopped", "finished"])
def test_an_ended_session_is_busy_for_60_s_then_says_nothing(ended):
    assert codes(verdict(e=sessions(("g1", 30.0, ended)))) == ["sessions.recent"]
    assert verdict(e=sessions(("g1", 60.04, ended))) == {"state": "idle", "reasons": []}
    assert verdict(e=sessions(("g1", 3000.0, ended)))["state"] == "idle"


@pytest.mark.parametrize("state", [None, "", "paused", "standby", 7, ["running"]])
def test_a_session_with_no_known_ended_state_counts_as_not_ended(state):
    out = verdict(e=sessions(("g1", 100.0, state)))
    assert out["state"] == "unknown" and codes(out) == ["sessions.silent"]
    # The row's status is a string or None; params carry a status only if it is one.
    shown = out["reasons"][0]["params"]
    assert ("status" in shown) == (isinstance(state, str) and bool(state))


def test_a_silent_session_is_told_apart_from_a_recent_one_in_the_text():
    text = verdict(e=sessions(("g1", 100.0, "running")))["reasons"][0]["text"]
    assert "g1" in text and "2 min" in text and "not ended" in text


def test_at_most_five_reasons_per_kind_are_given_for_sessions():
    rows = [(f"g{n}", 100.0 + n, "running") for n in range(8)]
    rows += [(f"h{n}", 5.0 + n, "running") for n in range(8)]
    out = verdict(e=sessions(*rows))
    assert [r["code"] for r in out["reasons"]] == ["sessions.recent"] * 5 + [
        "sessions.silent"
    ] * 5


def test_the_session_boundary_is_decided_before_the_age_is_rounded(tmp_path):
    # 59.96 s and 60.04 s both print as 60.0 s; only the unrounded age tells
    # them apart, in the part and in the verdict made from it.
    now = 1_800_000_000.0
    seen = {}
    for label, age in (("young", 59.96), ("old", 60.04)):
        root = tmp_path / label
        root.mkdir()
        write_session(root, "g1__plates", now, age, group="g1", state="running")
        part = S.sessions_part([str(root)], now)
        doc = S.document(now, marker(), part, units(), guard())
        assert doc["eval_sessions"]["sessions"][0]["age_s"] == 60.0
        seen[label] = codes(doc["robot_idle"])
    assert seen == {"young": ["sessions.recent"], "old": ["sessions.silent"]}


@pytest.mark.parametrize(
    "active, sub, expected, code",
    [
        ("active", "running", "busy", "units.active"),
        ("activating", "start", "busy", "units.active"),
        # Still running while it stops or reloads.
        ("deactivating", "stop-sigterm", "busy", "units.active"),
        ("reloading", "reload", "busy", "units.active"),
        ("failed", "failed", "idle", None),
        ("inactive", "dead", "idle", None),
        # Any other ActiveState is one nobody here has classified.
        ("maintenance", "dead", "unknown", "units.state_unknown"),
        ("refreshing", "running", "unknown", "units.state_unknown"),
        ("something-new", "x", "unknown", "units.state_unknown"),
    ],
)
def test_the_active_state_of_a_levi_aeri_unit_decides(active, sub, expected, code):
    name = "levi-aeri-policy-x.service"
    out = verdict(u=units((name, active, sub)))
    assert out["state"] == expected
    if code is None:
        assert out["reasons"] == []
        return
    assert [(r["source"], r["code"]) for r in out["reasons"]] == [("units", code)]
    assert name in out["reasons"][0]["text"]
    expected_params = {"name": name, "active": active}
    if code == "units.active":
        expected_params["sub"] = sub
    assert out["reasons"][0]["params"] == expected_params


@pytest.mark.parametrize(
    "kwargs, source, code",
    [
        (
            {"g": guard(state="unknown", detail="probe failed: OSError")},
            "guard",
            "guard.unreadable",
        ),
        ({"g": guard(quiet=None)}, "guard", "guard.unreadable"),
        ({"g": guard(quiet=False)}, "guard", "guard.not_quiet"),
        ({"g": guard(state="sampling")}, "guard", "guard.unreadable"),
        ({"g": {"state": "ok"}}, "guard", "guard.unreadable"),
        ({"g": None}, "guard", "guard.unreadable"),
        ({"g": {}}, "guard", "guard.unreadable"),
        ({"m": marker("unknown", None, "unreadable")}, "marker", "marker.unreadable"),
        ({"m": None}, "marker", "marker.unreadable"),
        (
            {"e": sessions(state="unknown", detail="no roots")},
            "eval_sessions",
            "sessions.unreadable",
        ),
        (
            {"u": units(state="unknown", detail="no systemctl")},
            "units",
            "units.unreadable",
        ),
    ],
)
def test_an_unknown_source_makes_the_verdict_unknown(kwargs, source, code):
    out = verdict(**kwargs)
    assert out["state"] == "unknown"
    assert [r["source"] for r in out["reasons"]] == [source]
    assert out["reasons"][0]["state"] == "unknown" and out["reasons"][0]["text"]
    assert codes(out) == [code] and out["reasons"][0]["params"] == {}


def test_busy_beats_unknown_and_both_are_explained():
    out = verdict(
        m=marker("present", 600.0),
        u=units(state="unknown", detail="no systemctl"),
    )
    assert out["state"] == "busy"
    assert [(r["source"], r["state"]) for r in out["reasons"]] == [
        ("marker", "busy"),
        ("units", "unknown"),
    ]


def test_a_busy_session_is_seen_even_when_another_file_was_unreadable():
    out = verdict(e=sessions(("g1", 5.0, "running"), state="unknown", detail="1 bad"))
    assert out["state"] == "busy"
    assert {(r["source"], r["state"]) for r in out["reasons"]} == {
        ("eval_sessions", "busy"),
        ("eval_sessions", "unknown"),
    }


def test_every_source_busy_gives_one_reason_each():
    out = verdict(
        g=guard(robot=["franka_server"]),
        m=marker("present", 5.0),
        e=sessions(("g1", 2.0, "running")),
        u=units(("levi-aeri-run-1.service", "active", "running")),
    )
    assert out["state"] == "busy"
    assert [r["source"] for r in out["reasons"]] == [
        "guard",
        "marker",
        "eval_sessions",
        "units",
    ]


NOW = 1_800_000_000.0


def session_doc(tmp_path, age, **fields):
    """The document for one session file ``age`` seconds old, read from disk."""
    root = tmp_path / f"root{len(list(tmp_path.iterdir()))}"
    root.mkdir()
    write_session(root, "g1__plates", NOW, age, group="g1", **fields)
    part = S.sessions_part([str(root)], NOW)
    doc = S.document(NOW, marker(), part, units(), guard())
    assert_schema(doc)
    return doc


@pytest.mark.parametrize("age", [100, 3000])
def test_a_running_session_gone_silent_is_listed_and_the_rig_is_unknown(tmp_path, age):
    doc = session_doc(tmp_path, age, state="running")
    assert [r["group"] for r in doc["eval_sessions"]["sessions"]] == ["g1"]
    assert doc["eval_sessions"]["state"] == "ok"
    assert doc["robot_idle"]["state"] == "unknown"
    assert [(r["code"], r["params"]) for r in doc["robot_idle"]["reasons"]] == [
        ("sessions.silent", {"group": "g1", "age_s": float(age), "status": "running"})
    ]


def test_an_ended_session_is_not_listed_after_ten_minutes_and_does_not_matter(tmp_path):
    doc = session_doc(tmp_path, 3000, state="finished")
    assert doc["eval_sessions"]["sessions"] == []
    assert doc["robot_idle"] == {"state": "idle", "reasons": []}
    # Listed while recent, and then silent about it once past a minute.
    doc = session_doc(tmp_path, 100, state="finished")
    assert [r["status"] for r in doc["eval_sessions"]["sessions"]] == ["finished"]
    assert doc["robot_idle"] == {"state": "idle", "reasons": []}


@pytest.mark.parametrize("state", [None, 5])
def test_a_session_file_without_a_readable_state_is_unknown(tmp_path, state):
    doc = session_doc(tmp_path, 100, state=state)
    assert doc["eval_sessions"]["sessions"][0]["status"] is None
    assert doc["robot_idle"]["state"] == "unknown"
    assert [(r["code"], r["params"]) for r in doc["robot_idle"]["reasons"]] == [
        ("sessions.silent", {"group": "g1", "age_s": 100.0})
    ]


def test_a_recent_session_is_busy_whatever_its_state(tmp_path):
    doc = session_doc(tmp_path, 30, state="running")
    assert doc["robot_idle"]["state"] == "busy"
    assert codes(doc["robot_idle"]) == ["sessions.recent"]


def test_the_document_rounds_ages_for_output_only():
    marker_part = marker("present", 90.04)
    sessions_part = sessions(("g1", 12.34, "running"))
    doc = S.document(NOW, marker_part, sessions_part, units(), guard())
    assert doc["marker"]["age_s"] == 90.0
    assert doc["eval_sessions"]["sessions"][0]["age_s"] == 12.3
    # The parts handed in are kept as they were (a cached read is reused).
    assert marker_part["age_s"] == 90.04
    assert sessions_part["sessions"][0]["age_s"] == 12.34
    assert [r["params"]["age_s"] for r in doc["robot_idle"]["reasons"]] == [90.0, 12.3]
    assert_schema(doc)


# One scenario for every code of the contract and the params it must give.
REASON_CASES = [
    ("marker.present", {"m": marker("present", 90.04)}, {"age_s": 90.0}),
    ("marker.old", {"m": marker("present", 13 * HOUR)}, {"age_s": 46800.0}),
    ("marker.unreadable", {"m": marker("unknown", None, "bad")}, {}),
    (
        "marker.no_folder",
        {"m": marker("unknown", None, "the marker folder does not exist")},
        {},
    ),
    (
        "sessions.recent",
        {"e": sessions(("g1", 12.34, "running"))},
        {"group": "g1", "age_s": 12.3, "status": "running"},
    ),
    (
        "sessions.recent",
        {"e": sessions(("g1", 5.0, None))},
        {"group": "g1", "age_s": 5.0},
    ),
    (
        "sessions.silent",
        {"e": sessions(("g1", 100.0, "homing"))},
        {"group": "g1", "age_s": 100.0, "status": "homing"},
    ),
    ("sessions.unreadable", {"e": sessions(state="unknown", detail="x")}, {}),
    (
        "units.active",
        {"u": units(("levi-aeri-a.service", "active", "running"))},
        {"name": "levi-aeri-a.service", "active": "active", "sub": "running"},
    ),
    (
        "units.state_unknown",
        {"u": units(("levi-aeri-a.service", "maintenance", "dead"))},
        {"name": "levi-aeri-a.service", "active": "maintenance"},
    ),
    ("units.unreadable", {"u": units(state="unknown", detail="x")}, {}),
    (
        "guard.robot_process",
        {"g": guard(robot=["franka_server", "ros2_control_node"])},
        {"names": "franka_server, ros2_control_node"},
    ),
    ("guard.controller_active", {"g": guard(controller_active=True)}, {}),
    ("guard.unreadable", {"g": guard(state="unknown", detail="x")}, {}),
    ("guard.not_quiet", {"g": guard(quiet=False, strained=["io"])}, {}),
    (
        "guard.old_sample",
        {"g": {"state": "unknown", "detail": "old", "code": "guard.old_sample"}},
        {},
    ),
    ("guard.truncated", {"g": guard(quiet=None, truncated=True)}, {}),
    ("guard.controller_value", {"g": guard(controller_active=0)}, {}),
]


@pytest.mark.parametrize(
    "code, kwargs, params", REASON_CASES, ids=[c[0] for c in REASON_CASES]
)
def test_every_reason_carries_its_code_and_flat_params(code, kwargs, params):
    found = [r for r in verdict(**kwargs)["reasons"] if r["code"] == code]
    assert len(found) == 1 and found[0]["params"] == params


def test_the_cases_cover_every_code_of_the_contract():
    assert {case[0] for case in REASON_CASES} == set(REASON_CODES)


def test_the_document_has_the_contract_shape():
    now = 1_800_000_000.0
    doc = S.document(now, marker(), sessions(), units(), guard())
    assert_schema(doc)
    assert doc["robot_idle"]["state"] == "idle"
    assert datetime.fromisoformat(doc["sampled_at"]).timestamp() == pytest.approx(
        now, abs=1
    )
    json.dumps(doc, allow_nan=False)


# ------------------------------------------------------------------ over the real guard


def real_guard(
    tmp_path,
    monkeypatch,
    *,
    process=None,
    psi_cpu=1.0,
    scan_cap=None,
    hidden=False,
    controller_active=None,
):
    """The ``guard`` part as the real setup probes build it, on a fake /proc
    (``Probes`` over a ``Context``, as ``tests/test_setup_probes.py`` does)."""
    proc = FakeProc(tmp_path / "proc")
    proc.process(7, "bash", ["bash"])
    proc.host(psi_cpu=psi_cpu)
    if process:
        proc.process(500, "python3", process)
    if scan_cap:
        for pid in range(200, 206):
            proc.process(pid, "x")
        monkeypatch.setattr(probes, "MAX_PIDS", scan_cap)
    if hidden:
        (proc.root / "self" / "mountinfo").write_text(
            "22 1 0:21 / /proc rw - proc proc rw,hidepid=2\n"
        )
    fields = {"proc": str(proc.root)}
    if controller_active is not None:
        health = tmp_path / "health.json"
        health.write_text(
            json.dumps(
                {
                    "updated_at_epoch": 2_000_000_000.0,
                    "controller_active": controller_active,
                }
            )
        )
        fields["health_file"] = str(health)
    return probe().status(probes.Context(**fields))["guard"]


@pytest.mark.parametrize(
    "case, quiet, state, reasons, reason_codes",
    [
        ({}, True, "idle", [], []),
        ({"psi_cpu": 40.0}, False, "unknown", ["unknown"], ["guard.not_quiet"]),
        (
            {"process": ["python3", "-m", "fr3_robot_server.franka_server_ros2"]},
            False,
            "busy",
            ["busy"],
            ["guard.robot_process"],
        ),
        ({"scan_cap": 3}, None, "unknown", ["unknown"], ["guard.truncated"]),
        ({"hidden": True}, None, "unknown", ["unknown"], ["guard.unreadable"]),
        (
            {"controller_active": True},
            True,
            "busy",
            ["busy"],
            ["guard.controller_active"],
        ),
    ],
    ids=[
        "no process, quiet host",
        "no process, strained host",
        "robot process found",
        "scan truncated",
        "processes hidden",
        "controller active",
    ],
)
def test_robot_idle_over_the_real_guard(
    tmp_path, monkeypatch, case, quiet, state, reasons, reason_codes
):
    # The rules run on what the real producer gives, not on a hand-built dict.
    guard = real_guard(tmp_path, monkeypatch, **case)
    assert guard["state"] == "ok" and guard["quiet"] is quiet
    out = verdict(g=guard)
    assert out["state"] == state
    assert [r["state"] for r in out["reasons"]] == reasons
    assert codes(out) == reason_codes
    assert {r["source"] for r in out["reasons"]} <= {"guard"}


def test_the_real_guard_shapes_that_decide_the_verdict(tmp_path, monkeypatch):
    # What the verdict relies on, stated once against the producer: a strained
    # host has quiet False and no robot process listed; a cut scan has quiet
    # None; a found process is listed by name only.
    strained = real_guard(tmp_path / "a", monkeypatch, psi_cpu=40.0)
    assert strained["robot"] == [] and strained["strained"]
    cut = real_guard(tmp_path / "b", monkeypatch, scan_cap=3)
    assert cut["robot"] == [] and cut["truncated"] is True
    found = real_guard(tmp_path / "c", monkeypatch, process=["franka_server_ros2"])
    assert [set(p) for p in found["robot"]] == [{"pid", "name"}]
