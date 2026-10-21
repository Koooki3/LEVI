"""Adopting a run after a restart of the service (``Supervisor.adopt``), under the
findings of the reviews of the first build:

* **latched only after a read that worked**: one failing read of the record at the
  first call hid a live D from ``active()`` (``levi stop``, the deploy, the arm
  stack) for the life of the process;
* a D that is **already gone** is over, and its ``terminal D exited`` line is
  written to ``terminal_d.events`` (the tools that wait for it, ``wait_request.sh``
  and ``res_monitor.py``, would otherwise wait for ever);
* the record names the **core that owns** the run; a second core that shares the
  folder does not signal, drive or end a D that a live core owns, and takes it
  over when that core is gone;
* the record carries the site values the run reads: a restarted service finds the
  run with a site file that does not parse;
* a D that has already answered a Ctrl+C from elsewhere is not sent another one.

The machine is the scripted one of ``aeri_machine.py``; the second supervisor is
a second process of the same account as far as the record, the identity of
processes and the signals go (``Host`` fakes)."""

import dataclasses
import json
import os

import pytest
from aeri_machine import NAME
from aeri_run_kit import (
    OTHER,
    at_forward,
    interrupt_line,
    machine_of,
    refusal,
    restarted,
    second_core,
    sigints,
    stop_from_elsewhere,
)

from levi.marvel0 import aeri_state as st
from levi.marvel0 import record, run


@pytest.fixture(name="m")
def machine(tmp_path, monkeypatch):
    mm = machine_of(tmp_path, monkeypatch)
    yield mm
    mm.close()


def events(m) -> list:
    return (m.runs / NAME / "terminal_d.events").read_text().strip().splitlines()


def found(sup):
    """What ``aeri_run_api.active()`` asks: adopt first, then the job row."""
    sup.adopt()
    return sup.active()


def broken_site():
    raise ValueError("site.toml: not valid TOML")


# --- latched only after a read that worked --------------------------------------------------


def test_a_failing_first_read_does_not_hide_a_live_d_for_ever(m, monkeypatch):
    at_forward(m)
    new = restarted(m)
    real = record.read_record
    broken = {"on": True}

    def flaky(directory):
        if broken["on"]:
            raise OSError(5, "Input/output error")
        return real(directory)

    monkeypatch.setattr(record, "read_record", flaky)
    job = found(new)
    # (not "no run": a record that cannot be read may be a D that is running)
    assert job is not None and job["id"] == "record-unreadable"
    assert new._adopted is False
    assert sigints(m) == [], "nothing was signalled on a read that did not work"
    broken["on"] = False
    job = found(new)
    assert job is not None and job["kind"] == "marvel0-aeri"
    assert new._adopted is True and len(sigints(m)) == 1


def test_a_corrupt_record_is_read_again_and_a_missing_one_is_final(m):
    at_forward(m)
    new = restarted(m)
    path = m.folder / "record.json"
    good = path.read_text()
    path.write_text("{not json")
    assert found(new)["id"] == "record-unreadable" and new._adopted is False
    path.write_text(good)
    assert found(new) is not None and new._adopted is True
    # no record at all: that is a result, not a failure
    empty = restarted(m)
    path.unlink()
    assert found(empty) is None and empty._adopted is True


def test_a_site_file_that_does_not_parse_does_not_stop_the_run_being_found(m):
    """The record carries the site values the run reads and where to look for
    it needs no site file."""
    at_forward(m)
    new = restarted(m, site_loader=broken_site)
    doc = new.view()
    assert new._adopted is True and doc["state"]["id"] in ("orphaned", "stopping")
    assert doc["d"]["alive"] is True and doc["keys"] == []
    assert new.active()["kind"] == "marvel0-aeri"
    assert len(sigints(m)) == 1
    assert any("当前正向子任务" in line for line in doc["screen"]["tail"])


def test_a_record_without_the_snapshot_needs_the_site_file_and_waits_for_it(m):
    """A record of an older build has no ``site``: until the site file reads, the
    run is seen to be alive (it blocks a second D) but not followed, nothing is
    sent to it, and the next call tries again."""
    at_forward(m)
    rec = m.record()
    rec.pop("site")
    (m.folder / "record.json").write_text(json.dumps(rec))
    state = {"ok": False}

    def loader():
        if not state["ok"]:
            raise ValueError("not yet")
        return m.site

    new = restarted(m, site_loader=loader)
    assert found(new)["kind"] == "marvel0-aeri"
    assert new._adopted is False and sigints(m) == []
    doc = new.view()
    assert doc["state"]["id"] == "unknown" and doc["d"]["alive"] is True
    # (no key; and Stop is still there: the run is not another core's)
    assert doc["keys"] == [] and doc["stop"]["allowed"] is True
    state["ok"] = True
    assert found(new) is not None and new._adopted is True
    assert len(sigints(m)) == 1
    assert new.view()["state"]["id"] in ("orphaned", "stopping")


# --- a D that is already gone ------------------------------------------------------------------


def test_an_adopted_d_that_is_gone_gets_its_exit_line_in_the_events_file(m):
    """After a hang-up chain (SIGKILL kills the launcher too) nobody wrote
    ``terminal D exited``: the tools that wait for it would wait for ``--max-hours``."""
    at_forward(m)
    assert events(m)[-1].split("] ", 1)[1].startswith("start: ")
    m.child.exits(-9)
    new = restarted(m)
    doc = new.view()
    assert doc["state"]["id"] == "ended"
    assert events(m)[-1].endswith("terminal D exited rc=unknown")
    assert new._rec["phase"] == "ended" and new.active() is None
    assert doc["after_exit"] is not None
    assert m.record()["phase"] == "ended" and m.record()["ended_epoch"] > 0
    assert any(r["event"] == "ended" for r in m.history())


def test_the_exit_of_the_launcher_is_in_that_line_when_it_wrote_one(m):
    at_forward(m)
    rec = m.record()
    (m.folder / "logs" / rec["exit_file"]).write_text(
        json.dumps({"pid": 1, "code": 130, "signal": None})
    )
    m.child.exits(0)
    restarted(m).adopt()
    assert events(m)[-1].endswith("terminal D exited rc=130")


def test_an_exit_line_is_not_written_twice(m):
    at_forward(m)
    (m.runs / NAME / "terminal_d.events").write_text(
        "[10:00:00] start: x\n[10:05:00] terminal D exited rc=0\n"
    )
    m.child.exits(0)
    restarted(m).adopt()
    assert sum("terminal D exited" in line for line in events(m)) == 1


def test_a_second_start_after_an_earlier_exit_gets_its_own_exit_line(m):
    """The file may hold an earlier session's exit (a terminal that was started
    again): it is the last start / exit line that tells whether D is over."""
    at_forward(m)
    (m.runs / NAME / "terminal_d.events").write_text(
        "[10:00:00] start: a\n[10:05:00] terminal D exited rc=0\n[10:06:00] start: b\n"
    )
    m.child.exits(-9)
    restarted(m).adopt()
    assert events(m)[-1].endswith("terminal D exited rc=unknown")
    assert sum("terminal D exited" in line for line in events(m)) == 2


def test_a_second_adoption_of_a_run_that_ended_changes_nothing(m):
    at_forward(m)
    m.child.exits(0)
    restarted(m).adopt()
    first = events(m)
    restarted(m).adopt()
    assert events(m) == first


# --- the owner of a run -----------------------------------------------------------------------


def test_the_record_names_the_core_that_owns_the_run(m):
    m.start()
    owner = m.record()["owner"]
    assert owner["pid"] == os.getpid() and owner["identity"] == run._identity(
        os.getpid()
    )


def test_a_run_owned_by_another_live_core_is_not_signalled_or_driven(m):
    at_forward(m)
    new, _alive = second_core(m)
    doc = new.view()
    assert sigints(m) == [], "no SIGINT at the adoption of a run another core owns"
    assert doc["state"]["id"] == "unknown" and doc["state"]["reason"] == "no_keyboard"
    assert doc["d"]["alive"] is True and doc["d"]["keyboard"] is False
    assert doc["keys"] == []
    assert doc["stop"]["allowed"] is False
    assert doc["stop"]["why_not"]["code"] == "driven_elsewhere"
    assert doc["end"]["allowed"] is False
    assert new.active()["kind"] == "marvel0-aeri", "it still blocks a second D"
    assert new._rec["stop_sent_epoch"] is None
    assert m.record()["owner"]["pid"] == os.getpid(), "the owner stays the owner"


def test_the_core_that_does_not_own_the_run_refuses_stop_and_end(m, monkeypatch):
    monkeypatch.setattr(st, "STALL_S", 0.01)
    at_forward(m)
    new, _alive = second_core(m)
    new.view()
    assert refusal(new.stop, {"request_id": m.rid()})[1] == "driven_elsewhere"
    assert (
        refusal(new.end, {"request_id": m.rid(), "confirm": "end terminal D"})[1]
        == "end_not_allowed"
    )
    # not even when the derivation fails (the blind Stop)
    new._current = lambda: (_ for _ in ()).throw(RuntimeError("a reader failed"))
    assert refusal(new.stop, {"request_id": m.rid()})[1] == "driven_elsewhere"
    assert [s for s in m.signals if s[0] in ("kill", "killpg")] == []
    assert m.typed() == b""


def test_the_core_that_does_not_own_the_run_watches_its_screen(m):
    at_forward(m)
    new, _alive = second_core(m)
    doc = new.view()
    assert any("当前正向子任务" in line for line in doc["screen"]["tail"])
    assert doc["progress"]["round"] == 1


def test_when_the_owner_is_gone_the_other_core_takes_the_run_and_signals_once(m):
    at_forward(m)
    new, alive = second_core(m)
    new.view()
    assert new._foreign is True and sigints(m) == []
    alive["first"] = False  # the first core is gone
    assert new.tick() is True  # (the watcher's look)
    assert new._foreign is False and new._adopted is True
    doc = new.view()
    assert doc["state"]["id"] in ("orphaned", "stopping")
    assert len(sigints(m)) == 1, "one SIGINT, now that nobody else drives it"
    assert new._rec["owner"] == OTHER and m.record()["owner"] == OTHER
    new.tick()
    new.adopt()
    assert len(sigints(m)) == 1


def test_a_foreign_run_that_ends_is_recorded_by_the_watcher(m):
    at_forward(m)
    new, _alive = second_core(m)
    new.view()
    m.child.exits(0)
    assert new.tick() is False
    assert new.view()["state"]["id"] == "ended" and new.active() is None


def test_a_record_with_no_owner_or_a_dead_one_is_taken_over(m):
    at_forward(m)
    rec = m.record()
    rec.pop("owner")
    (m.folder / "record.json").write_text(json.dumps(rec))
    new = restarted(m)
    new.adopt()
    assert new._foreign is False and len(sigints(m)) == 1
    assert new._rec["owner"]["pid"] == os.getpid()


@pytest.mark.parametrize(
    "owner",
    [
        None,
        "me",
        {},
        {"pid": "x", "identity": {}},
        {"pid": True, "identity": {"start_ticks": "1"}},
        {"pid": 5, "identity": None},
    ],
)
def test_an_owner_that_is_not_what_the_page_writes_does_not_hold_a_run(m, owner):
    at_forward(m)
    rec = m.record()
    rec["owner"] = owner
    (m.folder / "record.json").write_text(json.dumps(rec))
    new = restarted(m)
    new.adopt()
    assert new._foreign is False and len(sigints(m)) == 1


# --- a D that has answered a Ctrl+C already --------------------------------------------------------


def test_a_d_that_already_answered_a_ctrl_c_from_elsewhere_is_not_sent_another(m):
    at_forward(m)
    m.append_screen(interrupt_line())
    assert m.record()["stop_sent_epoch"] is None
    new = restarted(m)
    doc = new.view()
    assert sigints(m) == [], "a second Ctrl+C into a wrap-up kills its systemctl calls"
    assert doc["stop"]["phase"] == "acked" and doc["stop"]["allowed"] is False


def test_a_stop_the_script_sent_before_the_restart_is_not_sent_again(m):
    at_forward(m)
    stop_from_elsewhere(m)
    new = restarted(m)
    doc = new.view()
    assert sigints(m) == []
    assert doc["stop"]["phase"] in ("sent", "unconfirmed", "acked")
    assert doc["stop"]["allowed"] is False or doc["stop"]["phase"] == "unconfirmed"


def test_a_run_this_process_started_is_never_found_again_and_keeps_its_keyboard(m):
    """A first adoption that failed (not latched) and went on failing while a run
    was started here must not, once the read works again, turn that run into one
    "found after a restart": it would lose its keyboard (``keyboard=False``) and
    get a SIGINT."""
    real = m.sup._adopt_read

    def failing():
        raise OSError(5, "Input/output error")

    m.sup._adopt_read = failing
    m.sup.adopt()
    assert m.sup._adopted is False
    at_forward(m)  # (start() asks for the adoption too: it still fails, the run starts)
    assert m.sup._child is not None
    m.sup._adopt_read = real
    for _ in range(3):
        m.sup.adopt()
        doc = m.look()
    assert m.sup._found is False and m.sup._adopted is True
    assert doc["d"]["keyboard"] is True and doc["state"]["id"] == "forward_running"
    assert sigints(m) == []


# --- the second review: a record that cannot be read is "a D may be running" (N8) -----------------


def corrupt(m, text="{not json"):
    (m.folder / "record.json").write_text(text)


def unreadable_job(sup):
    sup.adopt()
    return sup.active()


def test_a_corrupt_record_is_a_job_for_the_gates_until_a_person_clears_it(m):
    """Safety review 2 N8 (probe P10): ``levi stop`` listed a loud
    ``record-unreadable`` row but ``aeri_run_api.active()`` said None and
    ``run_active()`` False, so E, the deploy and the arm stack went ahead."""
    at_forward(m)
    good = (m.folder / "record.json").read_text()
    corrupt(m)
    new = restarted(m)
    job = unreadable_job(new)
    assert job is not None and job["kind"] == "marvel0-aeri"
    assert job["id"] == "record-unreadable" and job["unreadable"] is True
    assert sigints(m) == [], "a record nobody can read is not acted on"
    # a person repairs it: the next look finds the run again
    (m.folder / "record.json").write_text(good)
    job = unreadable_job(new)
    assert job["id"] != "record-unreadable" and new._adopted is True
    assert len(sigints(m)) == 1
    # or removes it: no run, and then it is a result
    other = restarted(m)
    corrupt(m)
    assert unreadable_job(other)["id"] == "record-unreadable"
    (m.folder / "record.json").unlink()
    assert unreadable_job(other) is None and other._adopted is True


def test_an_unreadable_record_is_active_for_every_gate_of_the_product(m, monkeypatch):
    from levi import paths
    from levi.marvel0 import aeri_ready, aeri_run_api, deploy, run, run_api

    monkeypatch.setattr(paths, "workspace", lambda: m.workspace.root)
    at_forward(m)
    corrupt(m)
    new = restarted(m)
    monkeypatch.setattr(aeri_run_api, "supervisor", lambda: new)
    assert aeri_run_api.active()["id"] == "record-unreadable"
    assert run_api.run_active() is True, "(the arm stack's start and stop, E's start)"
    assert deploy._hosted_aeri()["id"] == "record-unreadable"
    assert run._hosted_aeri() is True
    assert aeri_ready._d_hosted_active() is True
    rows = __import__("levi.marvel0.aeri_activity", fromlist=["x"]).job_rows(
        m.workspace
    )
    assert [r["id"] for r in rows if r["kind"] == "marvel0-aeri"] == [
        "record-unreadable"
    ]


def test_the_document_of_an_unreadable_record_blocks_a_start_and_says_why(m):
    at_forward(m)
    corrupt(m)
    new = restarted(m)
    doc = new.view()
    assert doc["state"]["id"] == "idle"
    assert doc["start"]["allowed"] is False
    assert doc["start"]["why_not"]["code"] == "d_active"
    assert doc["start"]["why_not"]["params"] == {"record": "unreadable"}
    code = refusal(new.challenge, {"purpose": "start", "name": NAME})
    assert code[1] == "d_active" and code[2].get("record") == "unreadable"
    assert m.spawned and len(m.spawned) == 1, "no second D"


def test_a_start_is_not_possible_beside_a_record_nobody_can_read(m):
    corrupt_first = restarted(m)
    (m.folder.parent).mkdir(parents=True, exist_ok=True)
    m.folder.mkdir(parents=True, exist_ok=True)
    corrupt(m)
    assert unreadable_job(corrupt_first)["id"] == "record-unreadable"
    m.present()
    assert refusal(corrupt_first.challenge, {"purpose": "start", "name": NAME})[1] == (
        "d_active"
    )
    assert m.spawned == []


# --- one failed read of /proc does not end an adopted run (N7) -------------------------------------


def lost_once(real, kind="proc_state"):
    calls = {"n": 0}

    def read(pid):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(pid)

    read.calls = calls
    return read


@pytest.mark.parametrize("which", ["proc_state", "identity"])
def test_one_lost_read_does_not_end_an_adopted_run(m, which):
    at_forward(m)
    pauses = []
    host = dataclasses.replace(
        m.host,
        **{which: lost_once(getattr(m.host, which))},
        sleep=lambda s: pauses.append(s),
    )
    new = restarted(m, host=host)
    new.adopt()
    assert new._found is True and new._rec["phase"] == "started"
    assert len(sigints(m)) == 1, "found alive, told once"
    assert pauses and 0 < pauses[0] <= 1.0, "a short pause between the two reads"
    assert new.tick() is True
    assert new._rec["phase"] == "started"
    doc = new.view()
    assert doc["d"]["alive"] is True and doc["state"]["id"] == "orphaned"


def test_a_lost_read_in_a_look_does_not_end_an_adopted_run(m):
    at_forward(m)
    new = restarted(m)
    new.adopt()
    real = new.host.proc_state
    new.host = dataclasses.replace(new.host, proc_state=lost_once(real))
    assert new.tick() is True
    assert new._rec["phase"] == "started" and new.view()["d"]["alive"] is True


def test_two_lost_reads_in_a_row_do_end_an_adopted_run(m):
    """The launcher is gone for real: both reads miss."""
    at_forward(m)
    new = restarted(m)
    new.adopt()
    m.child.exits(-9)
    assert new.tick() is False
    assert new._rec["phase"] == "ended"
    assert new.active() is None


def test_a_pid_that_is_another_process_now_is_gone_at_once(m):
    """No second read is needed when the pid answers and is not the process the
    record names (it was reused)."""
    at_forward(m)
    pauses = []
    host = dataclasses.replace(
        m.host,
        identity=lambda pid: {"start_ticks": "999", "boot": "b"},
        sleep=lambda s: pauses.append(s),
    )
    new = restarted(m, host=host)
    new.adopt()
    assert new._rec["phase"] == "ended" and pauses == []
    assert sigints(m) == []


# --- two gaps of the adoption (mechanics review 2 N4) ------------------------------------------------


def test_a_record_with_no_launcher_identity_and_a_pid_that_is_gone_is_over(m):
    """(a): ``None == None`` counted as alive, and ``killpg`` went out with no
    proof of whose process group it was."""
    at_forward(m)
    rec = m.record()
    record.update_record(
        m.folder, leader={"pid": rec["leader"]["pid"], "identity": None}, owner=None
    )
    m.child.exits(0)  # (identity() answers None for every pid now)
    new = restarted(m)
    new.adopt()
    assert [s for s in m.signals if s[0] in ("kill", "killpg")] == []
    assert new._rec["phase"] == "ended" and new._found is False
    assert new.active() is None


def test_a_record_with_no_launcher_identity_is_never_signalled_even_if_the_pid_lives(m):
    at_forward(m)
    rec = m.record()
    record.update_record(
        m.folder, leader={"pid": rec["leader"]["pid"], "identity": None}, owner=None
    )
    new = restarted(m)
    new.adopt()
    assert [s for s in m.signals if s[0] in ("kill", "killpg")] == []


def test_a_second_core_does_not_end_a_launch_the_owner_is_still_making(m):
    """(b): the owner writes ``intent`` before the spawn and ``started`` after. A
    second core that looked in between marked the run ended and wrote ``terminal D
    exited rc=unknown`` after ``start:``."""
    at_forward(m)
    rec = record.read_record(m.folder)
    rec.pop("leader", None)
    rec["phase"] = "intent"
    record.write_record(m.folder, rec)
    before = events(m)
    new, _alive = second_core(m)
    new.adopt()
    after = record.read_record(m.folder)
    assert after["phase"] == "intent" and "exit" not in after
    assert events(m) == before, "no exit line was added"
    assert [s for s in m.signals if s[0] in ("kill", "killpg")] == []
    assert new.active() is not None, "and it still blocks a second D"
    assert new._foreign is True
    doc = new.view()
    assert doc["state"]["id"] != "ended" and doc["stop"]["allowed"] is False


def test_the_owner_going_on_to_started_is_not_disturbed_by_a_second_core(m):
    at_forward(m)
    rec = record.read_record(m.folder)
    leader = rec.pop("leader")
    rec["phase"] = "intent"
    record.write_record(m.folder, rec)
    new, _alive = second_core(m)
    new.adopt()
    record.update_record(m.folder, phase="started", leader=leader)
    assert new.tick() is True
    assert record.read_record(m.folder)["phase"] == "started"
    assert new.view()["d"]["alive"] is True
    assert sigints(m) == []
