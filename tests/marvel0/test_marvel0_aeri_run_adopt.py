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
import signal

import pytest
from aeri_machine import NAME
from aeri_run_kit import (
    at_forward,
    interrupt_line,
    machine_of,
    refusal,
    stop_from_elsewhere,
)

from levi.marvel0 import aeri_run, record, run
from levi.marvel0 import aeri_state as st


@pytest.fixture(name="m")
def machine(tmp_path, monkeypatch):
    mm = machine_of(tmp_path, monkeypatch)
    yield mm
    mm.close()


def restarted(m, host=None, site_loader=None):
    """A new process finds the record of the run: a new supervisor."""
    return aeri_run.Supervisor(
        host or m.host,
        site_loader=site_loader or (lambda: m.site),
        workspace=m.workspace,
    )


def sigints(m):
    return [s for s in m.signals if s[2] == signal.SIGINT]


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
    assert found(new) is None and new._adopted is False
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
    assert found(new) is None and new._adopted is False
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
    assert doc["keys"] == [] and doc["stop"]["allowed"] is False
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


OTHER = {"pid": 424242, "identity": {"start_ticks": "777", "boot": "other-boot"}}


def second_core(m, alive=None):
    """A second core: its own identity, and a view of the machine in which the
    first core (the test process) is alive for as long as ``alive['first']``."""
    alive = alive if alive is not None else {"first": True}
    first_pid = os.getpid()
    first_identity = run._identity(first_pid)

    def identity(pid):
        if pid == first_pid:
            return first_identity if alive["first"] else None
        return m.identity(pid)

    host = dataclasses.replace(m.host, self_owner=lambda: OTHER, identity=identity)
    return restarted(m, host=host), alive


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


def test_a_run_this_process_started_is_never_found_again_and_keeps_its_keyboard(
    m, monkeypatch
):
    """A first adoption that failed (not latched) must not, once a run is started
    here, turn that run into one "found after a restart": it would lose its
    keyboard (``keyboard=False``) and get a SIGINT."""
    real = record.read_record
    broken = {"on": True}

    def flaky(directory):
        if broken["on"]:
            raise OSError(5, "Input/output error")
        return real(directory)

    monkeypatch.setattr(record, "read_record", flaky)
    m.sup.adopt()
    assert m.sup._adopted is False
    broken["on"] = False
    at_forward(m)
    for _ in range(3):
        m.sup.adopt()
        doc = m.look()
    assert m.sup._found is False and m.sup._adopted is True
    assert doc["d"]["keyboard"] is True and doc["state"]["id"] == "forward_running"
    assert sigints(m) == []
