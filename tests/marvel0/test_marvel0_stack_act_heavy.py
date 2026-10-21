"""Heavy jobs and the wait for them (``stack_act.heavy_jobs_running``).

The decision "does a heavy job of the gate really run?" is the real function
here, on log files written in the shape ``heavy_gate.py`` writes them (its
``Gate._log``: the keys, the events, ``ts`` as ISO text with the offset) and
with ``systemctl --user show`` answered in the form the real one printed on the
machine (21 Oct): nothing in these tests replaces the decision itself. Only the
transport is a double: the runner that stands for ``systemctl``.

The last tests put the real function behind the page (``ActHost.heavy_jobs`` as
it is in production) and read what the document and the gates say."""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from stack_act_machine import ActMachine, scopes_print
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard
from test_marvel0_stack_act import start_sequence

from levi.marvel0 import control_site, stack, stack_act

NOW = 1_800_000_000.0
ZONE = timezone(timedelta(hours=8))
HUB_GATE = Path("/home/marvel/work/wenkai/LEVI/lab/levi-hub/heavy_gate.py")


def stamp(age_s):
    """``heavy_gate.now_iso()`` for a moment ``age_s`` before NOW."""
    when = datetime.fromtimestamp(NOW - age_s, tz=ZONE)
    return when.isoformat(timespec="seconds")


def unit_name(n=1, kind="pytest-dir"):
    return f"heavy-gate-{kind}-{2000000 + n}-1-{n:06x}"


def record(age_s, event, unit, kind="pytest-dir", **extra):
    """One line of the gate's log, with the keys ``Gate._log`` writes."""
    out = {
        "ts": stamp(age_s),
        "mono": 123456.0 - age_s,
        "kind": kind,
        "gate_id": "ab12cd34",
        "gate_pid": 424242,
        "unit": unit,
        "attempt": 1,
        "event": event,
        "criterion": None,
        "exit_code": None,
        "interrupted": False,
        "counts_as_evidence": False,
    }
    out.update(extra)
    return out


def launched(age_s, unit, kind="pytest-dir"):
    """``launching`` and ``start``, the two records written when a job begins."""
    return [
        record(age_s, "launching", unit, kind, mode="scope"),
        record(
            age_s,
            "start",
            unit,
            kind,
            mode="scope",
            pgid=777,
            pid=777,
            watch="inotify",
            cmd=["/usr/bin/pytest", "tests/x.py"],
            criterion={"state": "inactive", "why": "no socket"},
        ),
    ]


def write_log(path, records, tail=""):
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + tail)
    return path


class Systemd:
    """The runner that stands for ``systemctl --user show`` of scopes: the
    answers are the real command's output (``scopes_print``: one block per unit,
    ``Id`` in each, a blank line between them). ``asked`` lists the scopes asked
    about, call by call."""

    def __init__(self, scopes=None):
        self.scopes = scopes or {}
        self.asked = []
        self.calls = []
        self.fail = None

    def __call__(self, argv, timeout_s):
        self.calls.append(list(argv))
        assert argv[:4] == [
            "systemctl",
            "--user",
            "show",
            "--property=Id,LoadState,ActiveState,TasksCurrent",
        ], argv
        self.asked.extend(argv[4:])
        if self.fail == "error":
            return 1, "Failed to connect to bus"
        if self.fail == "raise":
            raise OSError("no systemctl")
        return 0, scopes_print(argv[4:], self.scopes)


@pytest.fixture
def log(tmp_path):
    return tmp_path / "heavy-gate.jsonl"


def heavy(log, systemd, now=NOW):
    return stack_act.heavy_jobs_running(log, systemd, lambda: now)


# --- the decision, on logs in the gate's own form ---------------------------------------------


def test_a_job_whose_scope_holds_processes_is_running(log):
    u = unit_name(1, "bun-validate")
    write_log(log, launched(60, u, "bun-validate"))
    systemd = Systemd({u + ".scope": ("loaded", "active", "9")})
    got = heavy(log, systemd)
    assert got["state"] == "running" and got["count"] == 1
    assert got["kinds"] == ["bun-validate"] and got["why"] is None
    assert systemd.asked == [u + ".scope"]


def test_a_finished_job_is_not_running_and_systemd_is_not_asked(log):
    u = unit_name(1)
    write_log(
        log,
        [*launched(900, u), record(60, "finish", u, exit_code=0, emptied=True)],
    )
    systemd = Systemd({u + ".scope": ("loaded", "active", "9")})  # (a stale scope)
    got = heavy(log, systemd)
    assert (got["state"], got["count"]) == ("clear", 0)
    assert systemd.asked == [], "a job that ended is not asked about"


def test_a_stale_empty_scope_is_not_a_job(log):
    """The scopes the gate left behind (loaded, active, no task): they are on
    this machine today; they must never hold A back."""
    stale = [unit_name(n, "bun-validate") for n in (1, 2, 3)]
    write_log(log, [r for u in stale for r in launched(120, u, "bun-validate")])
    systemd = Systemd({u + ".scope": ("loaded", "active", "0") for u in stale})
    got = heavy(log, systemd)
    assert (got["state"], got["count"], got["empty"]) == ("clear", 0, 3)


def test_a_scope_that_is_gone_is_not_a_job(log):
    u = unit_name(1)
    write_log(log, launched(30, u))
    assert heavy(log, Systemd())["state"] == "clear"


def test_a_job_the_gate_could_not_confirm_dead_counts_only_while_its_scope_has_processes(
    log,
):
    u = unit_name(1)
    write_log(log, [*launched(300, u), record(100, "not_empty", u, emptied=False)])
    busy = Systemd({u + ".scope": ("loaded", "active", "2")})
    assert heavy(log, busy)["state"] == "running"
    empty = Systemd({u + ".scope": ("loaded", "active", "0")})
    assert heavy(log, empty)["state"] == "clear"
    # and an "interrupted" the gate did not confirm empty ends nothing either
    write_log(
        log, [*launched(300, u), record(100, "interrupted", u, emptied="unknown")]
    )
    assert heavy(log, busy)["state"] == "running"


def test_a_requeue_after_an_interrupt_is_not_a_running_job(log):
    """The log's last line for the unit is ``queued`` (the gate lines up again
    with the unit field still set); the job it describes is over."""
    u = unit_name(1)
    write_log(
        log,
        [
            *launched(300, u),
            record(200, "inactive_window", u, seconds=1.0),
            record(100, "interrupted", u, emptied=True, interrupted=True),
            record(100, "queued", u, requeue=True),
        ],
    )
    systemd = Systemd({u + ".scope": ("loaded", "active", "4")})
    assert heavy(log, systemd)["state"] == "clear"
    assert systemd.asked == []


def test_a_queued_gate_that_never_launched_is_not_a_job(log):
    write_log(
        log, [record(50, "queued", None, cmd=["pytest"], stable=600, interval=30)]
    )
    assert heavy(log, Systemd())["state"] == "clear"


def test_a_job_last_heard_of_more_than_six_hours_ago_is_not_looked_at(log):
    old, recent = unit_name(1), unit_name(2)
    write_log(
        log,
        [
            *launched(stack_act.HEAVY_LOG_MAX_AGE_S + 60, old),
            *launched(stack_act.HEAVY_LOG_MAX_AGE_S - 600, recent),
        ],
    )
    systemd = Systemd(
        {
            old + ".scope": ("loaded", "active", "3"),
            recent + ".scope": ("loaded", "active", "3"),
        }
    )
    got = heavy(log, systemd)
    assert got["count"] == 1
    assert systemd.asked == [recent + ".scope"]


def test_units_that_are_not_the_gates_are_ignored(log):
    write_log(log, launched(30, "heavy-gate-probe-2876759-d59614"))
    systemd = Systemd()
    assert heavy(log, systemd)["state"] == "clear" and systemd.asked == []


# --- what cannot be told is never "clear" -------------------------------------------------------


def test_no_log_is_unknown(log):
    got = heavy(log, Systemd())
    assert (got["state"], got["why"]) == ("unknown", "log_missing")


def test_a_log_that_cannot_be_read_is_unknown(tmp_path):
    folder = tmp_path / "heavy-gate.jsonl"
    folder.mkdir()
    got = heavy(folder, Systemd())
    assert (got["state"], got["why"]) == ("unknown", "log_unreadable")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_a_log_without_permission_is_unknown(log):
    write_log(log, launched(30, unit_name(1)))
    log.chmod(0)
    try:
        got = heavy(log, Systemd())
    finally:
        log.chmod(0o644)
    assert (got["state"], got["why"]) == ("unknown", "log_unreadable")


def test_a_symbolic_link_for_a_log_is_not_followed(tmp_path):
    real = write_log(tmp_path / "real.jsonl", [])
    link = tmp_path / "heavy-gate.jsonl"
    link.symlink_to(real)
    assert heavy(link, Systemd())["state"] == "unknown"


@pytest.mark.parametrize("how", ["error", "raise"])
def test_a_systemd_that_does_not_answer_is_unknown(log, how):
    u = unit_name(1)
    write_log(log, launched(30, u))
    systemd = Systemd({u + ".scope": ("loaded", "active", "5")})
    systemd.fail = how
    got = heavy(log, systemd)
    assert (got["state"], got["why"]) == ("unknown", "systemd_unreadable")


def test_a_scope_without_a_task_count_is_unknown(log):
    u = unit_name(1)
    write_log(log, launched(30, u))
    systemd = Systemd({u + ".scope": ("loaded", "active", "[not set]")})
    assert heavy(log, systemd)["state"] == "unknown"
    huge = Systemd({u + ".scope": ("loaded", "active", str(2**64 - 1))})
    assert heavy(log, huge)["state"] == "unknown"


def test_a_job_that_ran_without_a_scope_cannot_be_told(log):
    u = unit_name(1)
    write_log(log, [record(30, "launching", u, mode="pgroup")])
    assert heavy(log, Systemd())["state"] == "unknown"


def test_a_job_that_is_seen_running_wins_over_one_that_cannot_be_told(log):
    seen, doubtful = unit_name(1), unit_name(2)
    write_log(log, [*launched(60, seen), *launched(50, doubtful)])
    systemd = Systemd(
        {
            seen + ".scope": ("loaded", "active", "3"),
            doubtful + ".scope": ("loaded", "active", "[not set]"),
        }
    )
    got = heavy(log, systemd)
    assert (got["state"], got["count"]) == ("running", 1)


def test_more_candidates_than_can_be_checked_are_unknown(log):
    units = [unit_name(n) for n in range(stack_act.HEAVY_MAX_UNITS + 1)]
    write_log(log, [r for u in units for r in launched(60, u)])
    systemd = Systemd()
    got = heavy(log, systemd)
    assert (got["state"], got["why"]) == ("unknown", "too_many_candidates")
    assert systemd.asked == []


# --- reading the log ----------------------------------------------------------------------------


def test_lines_that_are_not_json_are_skipped_as_the_gate_skips_them(log):
    u = unit_name(1)
    lines = [json.dumps(r) for r in launched(30, u)]
    log.write_text(
        "not json at all\n"
        + lines[0]
        + "\n\n[1, 2]\n"
        + lines[1]
        + '\n{"ts": "half a line'  # (a write cut short)
    )
    systemd = Systemd({u + ".scope": ("loaded", "active", "3")})
    assert heavy(log, systemd)["state"] == "running"


def test_a_long_log_is_read_from_its_end(log, monkeypatch):
    monkeypatch.setattr(stack_act, "HEAVY_LOG_TAIL_BYTES", 2000)
    old, now_u = unit_name(1), unit_name(2)
    filler = [record(5000, "queued", None, pad="x" * 80) for _ in range(60)]
    write_log(log, [*launched(7000, old), *filler, *launched(30, now_u)])
    assert log.stat().st_size > 2000
    systemd = Systemd(
        {
            old + ".scope": ("loaded", "active", "3"),
            now_u + ".scope": ("loaded", "active", "3"),
        }
    )
    got = heavy(log, systemd)
    assert got["count"] == 1 and systemd.asked == [now_u + ".scope"]


def test_a_log_that_has_not_changed_is_not_parsed_again(log, monkeypatch):
    u = unit_name(1)
    write_log(log, launched(30, u))
    systemd = Systemd({u + ".scope": ("loaded", "active", "3")})
    heavy(log, systemd)
    parsed = []
    real = json.loads
    monkeypatch.setattr(
        stack_act.json, "loads", lambda text: parsed.append(text) or real(text)
    )
    assert heavy(log, systemd)["state"] == "running" and parsed == []
    with log.open("a") as handle:  # a record is appended: parsed again
        handle.write(json.dumps(record(1, "finish", u, emptied=True)) + "\n")
    assert heavy(log, systemd)["state"] == "clear" and parsed


# --- the shape the gate writes (guard against drift) -------------------------------------------


def test_the_terms_match_the_gates_own_when_its_source_is_there():
    if not HUB_GATE.is_file():
        pytest.skip("heavy_gate.py is not on this machine")
    text = HUB_GATE.read_text()
    terminal = re.search(r"^TERMINAL = \(([^)]*)\)", text, re.MULTILINE).group(1)
    assert tuple(re.findall(r'"(\w+)"', terminal)) == stack_act.HEAVY_TERMINAL
    unit_re = re.search(r'^UNIT_RE = re\.compile\(r"\^(.*)\$"\)', text, re.MULTILINE)
    mine = stack_act.HEAVY_UNIT.pattern.replace(r"\.scope", "")
    assert unit_re.group(1).removesuffix(r"\.scope") == mine
    # the keys a record carries, and the two events that start a job
    for key in ("ts", "unit", "event", "emptied", "mode"):
        assert f'"{key}"' in text or f"{key}=" in text
    assert '"launching"' in text and '"start"' in text
    assert "MARKER_MIN_AGE = 2.0" in text


def test_the_log_is_the_gates_default_name_in_the_hub():
    if not HUB_GATE.is_file():
        pytest.skip("heavy_gate.py is not on this machine")
    assert f'"{stack_act.HEAVY_LOG_NAME}"' in HUB_GATE.read_text()


# --- behind the page: the real function, the document and the gates -----------------------------


@pytest.fixture
def m(tmp_path, monkeypatch):
    made = ActMachine(tmp_path)
    made.act.host.heavy_jobs = stack_act.heavy_jobs_running  # (as in production)
    # (a file's ctime cannot be set back: the marker's age is read from its
    # mtime, which the tests set)
    real_view = stack.marker_view
    monkeypatch.setattr(
        stack,
        "marker_view",
        lambda path, now: {**(v := real_view(path, now)), "ctime_age_s": v["age_s"]},
    )
    return made


def hub_log(m, records):
    return write_log(m.marker_path.parent / stack_act.HEAVY_LOG_NAME, records)


def with_marker(m, age=10.0):
    m.m_ready()
    m.set_marker_old(aged_s=age)
    m.preflight_a()
    m.no_manager()


def now_log(m, age_s, event, unit, **extra):
    """A record of the gate dated against the machine's clock."""
    return {**record(0, event, unit, **extra), "ts": stamp_at(m.t - age_s)}


def stamp_at(epoch):
    return datetime.fromtimestamp(epoch, tz=ZONE).isoformat(timespec="seconds")


def job(m, unit, age_s=30, kind="pytest-dir"):
    return [
        now_log(m, age_s, "launching", unit, kind=kind, mode="scope"),
        now_log(m, age_s, "start", unit, kind=kind, mode="scope"),
    ]


def test_a_running_job_holds_a_back_and_the_document_counts_it(m):
    with_marker(m, age=10.0)
    u = unit_name(1)
    hub_log(m, job(m, u))
    m.scopes[u + ".scope"] = ("loaded", "active", "6")
    marker = m.doc()["marker"]
    assert marker["heavy"]["state"] == "running" and marker["heavy"]["count"] == 1
    assert marker["remaining_s"] is None  # (until it yields: not known)
    assert "heavy_jobs_running" in m.blockers("a")
    row = next(
        b
        for b in m.terminal("a")["start_blockers"]
        if b["code"] == "heavy_jobs_running"
    )
    assert row["params"] == {"count": 1, "kinds": "pytest-dir"}
    assert "1 heavy job" in row["text"]
    # it ends: A is released at once, the marker being old enough
    m.scopes[u + ".scope"] = ("loaded", "active", "0")
    assert "heavy_jobs_running" not in m.blockers("a")
    assert "marker_young" not in m.blockers("a")


def test_a_stale_empty_scope_does_not_hold_a_back(m):
    with_marker(m, age=10.0)
    u = unit_name(1)
    hub_log(m, job(m, u))
    m.scopes[u + ".scope"] = ("loaded", "active", "0")
    marker = m.doc()["marker"]
    assert (marker["heavy"]["state"], marker["heavy"]["empty"]) == ("clear", 1)
    assert marker["remaining_s"] == 0
    assert not {"heavy_jobs_running", "marker_young"} & set(m.blockers("a"))


def test_without_a_log_the_fixed_wait_of_old_applies_and_the_document_says_so(m):
    with_marker(m, age=10.0)  # (no heavy-gate.jsonl in the hub)
    marker = m.doc()["marker"]
    assert marker["heavy"]["state"] == "unknown"
    assert marker["heavy"]["why"] == "log_missing"
    assert marker["wait_s"] == control_site.LEGACY_MARKER_WAIT_S
    assert marker["configured_wait_s"] == control_site.DEFAULT_MARKER_WAIT_S
    assert marker["remaining_s"] == pytest.approx(35.0, abs=0.2)
    assert "marker_young" in m.blockers("a")
    m.t += 40  # past the fixed wait, the unknown no longer holds A back
    m.no_manager()
    assert "marker_young" not in m.blockers("a")
    assert m.doc()["marker"]["heavy"]["state"] == "unknown"


def test_a_configured_wait_longer_than_the_fixed_one_is_kept_when_unknown(m):
    m.set_control(marker_wait_s=120.0)
    with_marker(m, age=10.0)
    assert m.doc()["marker"]["wait_s"] == 120.0


def test_the_heavy_jobs_are_not_asked_without_a_marker_nor_once_a_runs(m):
    m.m_ready()
    assert m.doc()["marker"]["heavy"] is None
    with_marker(m, age=10.0)
    hub_log(m, [])
    assert m.doc()["marker"]["heavy"]["state"] == "clear"
    m.act.start("a", m.start_body("a"))
    assert m.doc()["marker"]["heavy"] is None


def test_the_sequence_waits_for_a_running_job_then_goes_on(m):
    m.m_ready()
    u = unit_name(1)
    start_sequence(m)
    for _ in range(4):  # monitor, marker, probe: the marker is made, the checks run
        m.act.advance()
    hub_log(m, job(m, u))
    m.scopes[u + ".scope"] = ("loaded", "active", "6")
    m.t += 10
    m.no_manager()
    for _ in range(6):
        seq = m.act.advance()
    assert seq["state"] == "running" and seq["step"] == "wait"
    assert m.systemd_runs("a") == []
    doc = m.doc()["sequence"]
    assert doc["wait"]["heavy"]["count"] == 1
    assert doc["wait"]["holds"] == ["heavy_jobs_running"]
    m.scopes[u + ".scope"] = ("loaded", "active", "0")
    m.no_manager()
    for _ in range(6):
        seq = m.act.advance()
    assert len(m.systemd_runs("a")) == 1


def test_a_unit_that_is_not_ours_in_the_gates_log_changes_nothing(m):
    with_marker(m, age=10.0)
    hub_log(m, job(m, "heavy-gate-probe-2876759-d59614"))
    assert m.doc()["marker"]["heavy"]["state"] == "clear"


def test_the_page_never_asks_systemd_for_anything_but_a_show(m):
    with_marker(m, age=10.0)
    u = unit_name(1)
    hub_log(m, job(m, u))
    m.scopes[u + ".scope"] = ("loaded", "active", "6")
    m.doc()
    scope_calls = [c for c in m.calls if c[-1].endswith(".scope")]
    assert scope_calls and all(c[:4] == [
        "systemctl", "--user", "show", "--property=Id,LoadState,ActiveState,TasksCurrent"
    ] for c in scope_calls)  # fmt: skip
    assert stack.guarded(lambda argv, t: (0, ""))(scope_calls[0], 1) == (0, "")


# --- the log's shape, more ----------------------------------------------------------------------


def test_a_queued_record_with_a_unit_is_not_a_launch(log):
    u = unit_name(1)
    write_log(log, [record(50, "queued", u, requeue=True)])
    systemd = Systemd({u + ".scope": ("loaded", "active", "4")})
    assert heavy(log, systemd)["state"] == "clear" and systemd.asked == []


def tail_log(log, monkeypatch, extra=0):
    """A log whose last line is the only record of a job, read through a tail
    window of that line's length plus ``extra`` bytes."""
    u = unit_name(1)
    filler = [record(5000, "queued", None, pad="x" * 60) for _ in range(20)]
    last = record(30, "launching", u, mode="scope")
    write_log(log, [*filler, last])
    size = len((json.dumps(last) + "\n").encode())
    monkeypatch.setattr(stack_act, "HEAVY_LOG_TAIL_BYTES", size + extra)
    return Systemd({u + ".scope": ("loaded", "active", "4")})


def test_a_tail_that_begins_on_a_line_keeps_that_line(log, monkeypatch):
    systemd = tail_log(log, monkeypatch)
    assert heavy(log, systemd)["state"] == "running"


def test_a_tail_that_begins_in_a_line_drops_the_cut_line_and_keeps_the_next(
    log, monkeypatch
):
    systemd = tail_log(log, monkeypatch, extra=7)
    assert heavy(log, systemd)["state"] == "running"


def test_all_the_candidates_are_asked_about_in_one_call_and_each_gets_its_own_answer(
    log,
):
    units = [
        unit_name(n) for n in range(1, 8)
    ]  # (as many stale scopes as there are today)
    write_log(log, [r for u in units for r in launched(120, u)])
    states = {u + ".scope": ("loaded", "active", "0") for u in units}
    states[units[3] + ".scope"] = ("loaded", "active", "5")  # (one really runs)
    systemd = Systemd(states)
    got = heavy(log, systemd)
    assert (got["state"], got["count"], got["empty"]) == ("running", 1, 6)
    assert len(systemd.calls) == 1, "one systemctl for all, not one each"
    assert sorted(systemd.asked) == sorted(u + ".scope" for u in units)


def test_a_unit_that_systemd_gave_no_block_for_is_unknown(log):
    u = unit_name(1)
    write_log(log, launched(30, u))
    systemd = Systemd()
    systemd.fail = None
    real = systemd.__call__

    def silent(argv, timeout_s):
        real(argv, timeout_s)
        return 0, ""  # (exit 0 and nothing printed)

    got = heavy(log, silent)
    assert (got["state"], got["why"]) == ("unknown", "systemd_unreadable")


def test_the_real_machines_output_for_several_scopes_is_read_as_it_is_printed(log):
    """The output captured on this machine (21 Oct) for one ``show`` of three
    scopes: the empty one that is left over, one that is not there, another
    left-over one; the property order is systemd's, not the request's."""
    stale1, gone, stale2 = unit_name(1, "bun-validate"), unit_name(2), unit_name(3)
    write_log(log, [r for u in (stale1, gone, stale2) for r in launched(120, u)])
    captured = (
        f"TasksCurrent=0\nId={stale1}.scope\nLoadState=loaded\nActiveState=active\n"
        f"\nTasksCurrent=[not set]\nId={gone}.scope\nLoadState=not-found\n"
        "ActiveState=inactive\n"
        f"\nTasksCurrent=0\nId={stale2}.scope\nLoadState=loaded\nActiveState=active\n"
    )
    got = heavy(log, lambda argv, t: (0, captured))
    assert (got["state"], got["count"], got["empty"]) == ("clear", 0, 2)
