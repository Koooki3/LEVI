"""Stop of the hosted terminal D, and the state around it, under the failures the
reviews of the first build found (``aeri_run.py``):

* a Ctrl+C sent from **elsewhere** (the out-of-page script, a terminal, ``kill``)
  is known to the page: it does not offer Stop again into a wrap-up that has begun
  (a second Ctrl+C kills the ``systemctl`` calls D's shutdown is made of);
* the page writes the record **merged** into what is on disk, so the field the
  script wrote is not lost;
* a Stop that is repeated goes to D's checked pid, never again by the pty;
* **the site file is not read while a run lives**: an ``[aeri]`` section or a
  site file edited into something invalid in the middle of a run neither turns the
  live D into ``ended`` nor takes Stop away; and when anything about the
  derivation of the state fails, Ctrl+C still goes to D's pid;
* the words by phase: until D has answered, the arm may still move.

The machine is the scripted one of ``aeri_machine.py`` at moments of the real run
of 18 Oct (r08); the script that writes the record is the real one."""

import dataclasses
import fcntl
import json
import os
import signal
import stat
import threading
import time

import pytest
from aeri_run_kit import (
    at_forward,
    at_release,
    interrupt_line,
    machine_of,
    operator_interrupt_note,
    refusal,
    restarted,
    sigints,
    stop_from_elsewhere,
    stop_script,
)
from fastapi import HTTPException

from levi.marvel0 import aeri_run
from levi.marvel0 import aeri_state as st


@pytest.fixture(name="m")
def machine(tmp_path, monkeypatch):
    mm = machine_of(tmp_path, monkeypatch)
    yield mm
    mm.close()


def stop_body(m):
    return {"request_id": m.rid()}


# --- a Stop sent from elsewhere ------------------------------------------------------------


def test_a_stop_sent_by_the_script_is_known_to_the_page_and_not_offered_again(m):
    """The review's case: the script wrote ``stop_sent_epoch`` and D printed its
    interrupt line; the page offered Stop again and a second ``\\x03`` went out."""
    at_forward(m)
    before = m.look()
    assert before["stop"]["allowed"] is True and before["stop"]["phase"] == "idle"
    stop_from_elsewhere(m)
    doc = m.look()
    assert doc["state"]["id"] == "stopping" and doc["stop"]["phase"] == "sent"
    assert doc["stop"]["allowed"] is False and doc["stop"]["sent_at"] is not None
    assert doc["stop"]["why_not"]["code"] == "stop_spent"
    m.append_screen(interrupt_line())
    doc = m.look()
    assert doc["stop"]["phase"] == "acked" and doc["stop"]["allowed"] is False
    assert refusal(m.sup.stop, stop_body(m))[1] == "stop_spent"
    assert m.typed() == b"", "no second Ctrl+C went to the pty"
    assert not [s for s in m.signals if s[2] == signal.SIGINT]


def test_the_script_and_the_page_do_not_both_send(m):
    """Between the record and the page's own click there is no moment in which
    the page does not know: it reads the record in every look, also in the one
    its Stop makes."""
    at_forward(m)
    stop_from_elsewhere(m)
    m.sup._cache = None  # (no document was read since the script ran)
    assert refusal(m.sup.stop, stop_body(m))[1] == "stop_spent"
    assert m.typed() == b""


def test_an_interrupt_line_from_d_is_an_acknowledgement_whoever_sent_the_signal(m):
    """A Ctrl+C typed in a terminal, a ``kill``: the page never sent one and
    nothing is in the record; D's own line is the evidence."""
    at_forward(m)
    assert m.record()["stop_sent_epoch"] is None
    m.append_screen(interrupt_line("SIGTERM"))
    doc = m.look()
    assert doc["state"]["id"] == "stopping" and doc["stop"]["phase"] == "acked"
    assert doc["stop"]["allowed"] is False and doc["state"]["may_move"] is False
    assert refusal(m.sup.stop, stop_body(m))[1] == "stop_spent"
    assert m.typed() == b""


def test_the_journals_note_of_an_interrupt_is_the_same_evidence(m):
    at_forward(m)
    m.append_journal([operator_interrupt_note()])
    doc = m.look()
    assert doc["state"]["id"] == "stopping" and doc["stop"]["phase"] == "acked"
    assert doc["stop"]["allowed"] is False
    assert refusal(m.sup.stop, stop_body(m))[1] == "stop_spent"
    assert m.typed() == b""


def test_a_stop_the_script_sent_that_d_never_answers_may_be_retried_once_by_pid(
    m, monkeypatch
):
    monkeypatch.setattr(st, "STOP_ACK_S", 0.2)
    at_forward(m)
    stop_from_elsewhere(m)
    time.sleep(0.4)
    doc = m.look()
    assert doc["stop"]["phase"] == "unconfirmed" and doc["stop"]["allowed"] is True
    answer = m.stop()
    assert answer["phase"] == "retried" and answer["via"] == "pid"
    assert m.typed() == b"", "the retry does not go by the pty"
    assert ("kill", m.child.pid, signal.SIGINT) in m.signals
    assert refusal(m.sup.stop, stop_body(m))[1] == "stop_spent"


def test_a_second_sigint_of_the_script_uses_up_the_retry(m):
    """``aeri_page_stop.py --again`` after the page's own Stop: two signals have
    gone out, the page offers no third."""
    at_forward(m)
    m.stop()
    assert m.typed() == b"\x03"
    first = m.record()["stop_sent_epoch"]
    stop_from_elsewhere(m, when=first + 7.0)
    doc = m.look()
    assert doc["stop"]["phase"] == "retried" and doc["stop"]["allowed"] is False
    assert refusal(m.sup.stop, stop_body(m))[1] == "stop_spent"
    assert m.typed() == b""
    assert m.record()["stop_retried"] is True, "kept for a page that is restarted"


def test_the_pages_writes_keep_what_the_script_wrote(m):
    """``write_record`` of the whole in-memory record erased the script's field
    (and the page never read it back). The record is written merged."""
    at_forward(m)
    stop_from_elsewhere(m)
    assert m.sup._rec["stop_sent_epoch"] is None  # (the page has not looked yet)
    m.sup._update(adopted_at=1.0)  # any write of the page
    on_disk = m.record()
    assert on_disk["stop_sent_epoch"] is not None and on_disk["adopted_at"] == 1.0
    assert m.sup._rec["stop_sent_epoch"] == on_disk["stop_sent_epoch"]


def test_a_stop_the_page_sends_is_in_the_record_for_the_script_to_see(m):
    at_forward(m)
    m.stop()
    rec = stop_script().read_json(m.folder / "record.json")
    assert rec["stop_sent_epoch"] > 0 and rec["run_id"] == m.record()["run_id"]


# --- the words by phase: the arm may move until D has answered -------------------------------


def test_until_d_has_answered_the_arm_may_still_move(m, monkeypatch):
    """Contract for the texts (``stop.phase`` and ``state.may_move``): ``sent``,
    ``unconfirmed`` and ``retried`` are "sent, not confirmed; the arm may still
    move"; only ``acked`` is "winding down, the arm stays where it is"."""
    monkeypatch.setattr(st, "STOP_ACK_S", 0.2)
    at_forward(m)
    m.stop()
    doc = m.look()
    assert (doc["stop"]["phase"], doc["state"]["id"]) == ("sent", "stopping")
    assert doc["state"]["may_move"] is True
    time.sleep(0.4)
    doc = m.look()
    assert doc["stop"]["phase"] == "unconfirmed" and doc["state"]["may_move"] is True
    m.stop()
    doc = m.look()
    assert doc["stop"]["phase"] == "retried" and doc["state"]["may_move"] is True
    m.append_screen(interrupt_line())
    doc = m.look()
    assert doc["stop"]["phase"] == "acked" and doc["state"]["may_move"] is False
    assert doc["state"]["id"] == "stopping" and doc["state"]["reason"] == "stop_sent"


def test_a_stalled_wrap_up_that_never_answered_may_still_move_the_arm(m, monkeypatch):
    monkeypatch.setattr(st, "STALL_S", 0.3)
    at_forward(m)
    m.stop()
    time.sleep(0.5)
    doc = m.look()
    assert doc["state"]["id"] == "stalled" and doc["state"]["may_move"] is True


def test_a_stalled_wrap_up_that_had_answered_holds_the_arm(m, monkeypatch):
    monkeypatch.setattr(st, "STALL_S", 0.3)
    at_forward(m)
    m.stop()
    m.append_screen(interrupt_line())
    m.look()
    time.sleep(0.5)
    doc = m.look()
    assert doc["state"]["id"] == "stalled" and doc["state"]["may_move"] is False


# --- the site file is not read while a run lives ---------------------------------------------


def break_the_site(m, how):
    if how == "aeri_section":  # the review's case: [aeri] edited into an invalid one
        m.site = dataclasses.replace(m.site, aeri=None)
    elif how == "refuse":

        def refused():
            raise st.Refuse("aeri_disabled")

        m.sup.site_loader = refused
    elif how == "http":

        def invalid():
            raise HTTPException(412, "the site file is invalid")

        m.sup.site_loader = invalid
    else:

        def unreadable():
            raise ValueError("site.toml: not valid TOML")

        m.sup.site_loader = unreadable


BROKEN = ["aeri_section", "refuse", "http", "toml"]


@pytest.mark.parametrize("how", BROKEN)
def test_the_state_and_stop_survive_a_broken_site_file(m, how):
    at_forward(m)
    break_the_site(m, how)
    doc = m.look()
    assert doc["state"]["id"] == "forward_running", doc["state"]
    assert doc["d"]["alive"] is True and doc["stop"]["allowed"] is True
    assert {k["id"] for k in doc["keys"] if k["allowed"]} == {
        "verdict_success",
        "verdict_failure",
        "verdict_discard",
    }
    answer = m.stop()
    assert answer["accepted"] is True and answer["via"] == "pty"
    assert m.typed() == b"\x03"
    doc = m.look()
    assert doc["state"]["id"] == "stopping" and doc["stop"]["phase"] == "sent"


@pytest.mark.parametrize("how", BROKEN)
def test_a_key_still_goes_to_the_prompt_the_person_saw_when_the_site_file_broke(m, how):
    at_release(m)
    break_the_site(m, how)
    m.press("release_round")
    assert m.typed() == b"\r"


@pytest.mark.parametrize("how", BROKEN)
def test_the_watcher_keeps_looking_when_the_site_file_broke(m, how):
    at_forward(m)
    break_the_site(m, how)
    assert m.sup.tick() is True
    assert m.sup.active()["kind"] == "marvel0-aeri"
    m.child.exits(0)
    assert m.sup.tick() is False
    doc = m.doc()
    assert doc["state"]["id"] == "ended" and doc["state"]["reason"] == "exit_0"
    assert doc["after_exit"] is not None
    # (and the next start is not possible: the card is off while the file is bad)
    assert doc["start"]["allowed"] is False


def test_the_record_carries_what_the_run_reads_of_the_site(m):
    m.start()
    rec = m.record()
    site = aeri_run.RunSite.from_json(rec["site"])
    assert site == aeri_run.RunSite.of(m.site, m.aeri)
    assert site.rollout_root == str(m.rollout) and site.runs_root == str(m.runs)
    assert site.gpu_lock_file == str(m.site.gpu_lock_file)
    assert site.avoid and all(isinstance(a, str) for a in site.avoid)
    assert stat.S_IMODE(os.stat(m.folder / "record.json").st_mode) == 0o600


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "x",
        {},
        {"rollout_root": "/a", "runs_root": "/b", "console_lang": "zh"},
        {
            "rollout_root": "/a",
            "runs_root": "",
            "console_lang": "zh",
            "gpu_lock_file": "/c",
            "avoid": [],
        },
        {
            "rollout_root": "/a",
            "runs_root": "/b",
            "console_lang": "zh",
            "gpu_lock_file": "/c",
            "avoid": "/x",
        },
        {
            "rollout_root": "/a",
            "runs_root": "/b",
            "console_lang": "zh",
            "gpu_lock_file": "/c",
            "avoid": [1],
        },
    ],
)
def test_a_snapshot_that_is_not_what_the_page_writes_is_not_trusted(bad):
    assert aeri_run.RunSite.from_json(bad) is None


# --- Stop when the state cannot be derived at all ----------------------------------------------


def broken_derivation(m):
    def boom():
        raise RuntimeError("a reader failed")

    m.sup._current = boom


def test_stop_goes_to_d_when_the_derivation_fails_and_only_once(m):
    at_forward(m)
    broken_derivation(m)
    answer = m.stop()
    assert answer["accepted"] is True and answer["via"] == "pty"
    assert m.typed() == b"\x03"
    assert m.record()["stop_sent_epoch"] is not None
    assert refusal(m.sup.stop, stop_body(m))[1] == "stop_spent"
    assert m.typed() == b""


def test_a_blind_stop_falls_back_to_the_checked_pid(m):
    at_forward(m)
    m.child.isig(False)  # the pty would not turn ^C into a signal
    broken_derivation(m)
    answer = m.stop()
    assert answer["via"] == "pid" and ("kill", m.child.pid, signal.SIGINT) in m.signals
    assert m.typed() == b""


def test_a_blind_stop_never_signals_a_pid_whose_identity_does_not_match(m):
    at_forward(m)
    m.child.isig(False)
    body = json.loads((m.run_dir / "runner.json").read_text())
    body["identity"] = {"boot": "b", "start_ticks": "999"}
    (m.run_dir / "runner.json").write_text(json.dumps(body))
    m.host.tcgetpgrp = lambda fd: 4242
    broken_derivation(m)
    answer = m.stop()
    assert answer["via"] == "group" and not [s for s in m.signals if s[0] == "kill"]


def test_a_blind_stop_of_a_run_that_is_over_is_refused(m):
    at_forward(m)
    m.child.exits(0)
    m.sup.tick()
    broken_derivation(m)
    assert refusal(m.sup.stop, stop_body(m))[1] == "d_gone"


def test_the_blind_stop_does_not_need_a_name_the_site_file_gives(m):
    at_forward(m)
    broken_derivation(m)
    break_the_site(m, "toml")
    assert m.stop()["accepted"] is True
    assert m.history()[-1]["event"] == "stop" and m.history()[-1]["byte"] == "Ctrl+C"


# --- the second review: the retry for every wrap-up, a bounded End -----------------------------


def test_an_adopted_d_that_did_not_answer_the_sigint_of_the_adoption_gets_one_retry(
    m, monkeypatch
):
    """Safety review 2 N1 (probe P1): after the adoption's SIGINT and 5 s without
    an interrupt line the state was ``orphaned``, ``stop.allowed`` False with
    ``stop_spent``, ``end.allowed`` False: no route left but the script."""
    monkeypatch.setattr(st, "STOP_ACK_S", 0.2)
    at_forward(m)
    new = restarted(m)
    new.adopt()
    assert len(sigints(m)) == 1
    time.sleep(0.4)
    new._cache = None
    doc = new.view()
    assert doc["state"]["id"] == "orphaned" and doc["state"]["may_move"] is True
    assert doc["stop"]["phase"] == "unconfirmed" and doc["stop"]["allowed"] is True
    answer = new.stop({"request_id": m.rid()})
    assert answer["phase"] == "retried" and answer["via"] == "pid"
    assert len(sigints(m)) == 2, "to the identity-checked runner pid"
    assert all(s[0] == "kill" and s[1] == m.child.pid for s in sigints(m))
    assert m.typed() == b"", "an adopted run has no keyboard"
    doc = new.view()
    assert doc["stop"]["phase"] == "retried" and doc["stop"]["allowed"] is False
    assert refusal(new.stop, stop_body(m))[1] == "stop_spent"
    assert len(sigints(m)) == 2, "one retry, not a third"


def test_the_retry_of_an_adopted_d_never_goes_to_a_pid_whose_identity_differs(
    m, monkeypatch
):
    monkeypatch.setattr(st, "STOP_ACK_S", 0.2)
    at_forward(m)
    new = restarted(m)
    new.adopt()
    body = json.loads((m.run_dir / "runner.json").read_text())
    body["identity"] = {"boot": "b", "start_ticks": "999"}  # a pid that was reused
    (m.run_dir / "runner.json").write_text(json.dumps(body))
    time.sleep(0.4)
    new._cache = None
    assert new.view()["stop"]["allowed"] is True
    assert refusal(new.stop, stop_body(m))[1] == "signal_failed"
    assert len(sigints(m)) == 1, "nothing went to the other process"
    assert new._rec.get("stop_retried") is not True, "and the retry is not spent"


def test_a_stalled_wrap_up_that_never_answered_gets_the_retry_too(m, monkeypatch):
    monkeypatch.setattr(st, "STOP_ACK_S", 0.2)
    monkeypatch.setattr(st, "STALL_S", 0.4)
    at_forward(m)
    m.stop()
    time.sleep(0.6)
    doc = m.look()
    assert doc["state"]["id"] == "stalled"
    assert doc["stop"]["phase"] == "unconfirmed" and doc["stop"]["allowed"] is True
    answer = m.stop()
    assert answer["phase"] == "retried" and answer["via"] == "pid"
    assert m.typed() == b"\x03", "(the first one only went by the pty)"
    assert refusal(m.sup.stop, stop_body(m))[1] == "stop_spent"


def test_end_opens_after_the_retry_for_a_d_that_ignores_both_and_keeps_printing(
    m, monkeypatch
):
    """The bounded rule (N1): ``END_AFTER_RETRY_S`` after the retry with no
    acknowledgement. Printing steps is progress, so ``stalled`` never comes for
    such a D; without this End was unreachable for ever."""
    monkeypatch.setattr(st, "STOP_ACK_S", 0.2)
    monkeypatch.setattr(st, "END_AFTER_RETRY_S", 0.6)
    at_forward(m)
    m.stop()
    time.sleep(0.8)  # (longer than the minute: it counts from the retry, not from here)
    m.look()
    assert m.stop()["phase"] == "retried"
    assert m.doc()["end"]["allowed"] is False
    assert refusal(m.end)[1] == "end_not_allowed"
    started = time.monotonic()
    n = 0
    while time.monotonic() - started < 0.9:
        n += 1
        m.append_screen(f"forward step {n}\r\n")  # D goes on, as if nothing was sent
        doc = m.look()
        if doc["end"]["allowed"]:
            break
        time.sleep(0.05)
    assert doc["end"]["allowed"] is True
    assert doc["state"]["id"] == "stopping", "it is not stalled: it prints"
    assert doc["stop"]["phase"] == "retried"
    assert m.end()["accepted"] is True
    assert [s for s in m.signals if s[2] == signal.SIGKILL]
    assert m.doc()["state"]["id"] == "ended"


def test_end_does_not_open_after_the_retry_when_d_answered(m, monkeypatch):
    monkeypatch.setattr(st, "STOP_ACK_S", 0.2)
    monkeypatch.setattr(st, "END_AFTER_RETRY_S", 0.3)
    at_forward(m)
    m.stop()
    time.sleep(0.3)
    m.look()
    m.stop()
    m.append_screen(interrupt_line())
    time.sleep(0.5)
    m.append_screen("7/7 收尾：停止 VLM\r\n")
    doc = m.look()
    assert doc["stop"]["phase"] == "acked" and doc["end"]["allowed"] is False


def test_end_does_not_open_before_the_retry_was_used(m, monkeypatch):
    monkeypatch.setattr(st, "STOP_ACK_S", 0.2)
    monkeypatch.setattr(st, "END_AFTER_RETRY_S", 0.3)
    at_forward(m)
    m.stop()
    time.sleep(0.8)
    m.append_screen("forward step 1\r\n")
    doc = m.look()
    assert doc["stop"]["phase"] == "unconfirmed" and doc["stop"]["allowed"] is True
    assert doc["end"]["allowed"] is False, "the retry comes first"


def test_the_retry_time_survives_a_restart_of_the_page(m, monkeypatch):
    monkeypatch.setattr(st, "STOP_ACK_S", 0.2)
    monkeypatch.setattr(st, "END_AFTER_RETRY_S", 0.5)
    at_forward(m)
    m.stop()
    time.sleep(0.3)
    m.look()
    m.stop()
    assert m.record()["stop_retried"] is True and m.record()["stop_retry_epoch"] > 0
    before = len(sigints(m))  # (the retry: the first one went by the pty)
    new = restarted(m)
    new.adopt()
    assert len(sigints(m)) == before, "a restart sends nothing more"
    new._cache = None
    assert new.view()["end"]["allowed"] is False
    time.sleep(0.6)
    new.tick()
    new._cache = None
    assert new.view()["end"]["allowed"] is True


# --- a run the page cannot follow still has Stop (N12) ------------------------------------------


def broken_site():
    raise ValueError("site.toml: not valid TOML")


def drop_the_snapshot(m):
    rec = m.record()
    rec.pop("site")
    (m.folder / "record.json").write_text(json.dumps(rec))


def test_a_record_without_the_snapshot_and_a_site_that_does_not_read_still_has_stop(m):
    """Safety review 2 N12 (probe P3): the document said ``stop.allowed`` False
    with ``driven_elsewhere`` ("other core") while the backend Stop would send."""
    at_forward(m)
    drop_the_snapshot(m)
    new = restarted(m, site_loader=broken_site)
    doc = new.view()
    assert doc["d"]["alive"] is True and doc["state"]["id"] == "unknown"
    assert doc["state"]["reason"] == "no_keyboard" and doc["keys"] == []
    assert doc["stop"]["allowed"] is True and doc["stop"]["why_not"] is None
    assert doc["end"]["allowed"] is False
    assert sigints(m) == [], "looking sends nothing"
    answer = new.stop({"request_id": m.rid()})
    assert answer["accepted"] is True and answer["via"] == "pid"
    assert ("kill", m.child.pid, signal.SIGINT) in m.signals
    doc = new.view()
    assert doc["stop"]["allowed"] is False
    assert doc["stop"]["why_not"]["code"] == "stop_spent"
    assert doc["stop"]["why_not"]["code"] != "driven_elsewhere"
    assert refusal(new.stop, stop_body(m))[1] == "stop_spent"
    assert len(sigints(m)) == 1


def test_a_run_another_core_owns_is_still_not_stopped_from_here(m):
    from aeri_run_kit import second_core

    at_forward(m)
    new, _alive = second_core(m)
    doc = new.view()
    assert doc["stop"]["allowed"] is False
    assert doc["stop"]["why_not"]["code"] == "driven_elsewhere"


# --- the page's Stop writes the record before it signals, under the record's lock (N13) -------


def lock_is_held_by_the_page(m) -> bool:
    fd = os.open(m.folder / "record.json.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def test_the_page_writes_the_record_before_it_signals_and_holds_the_lock(m):
    """Safety review 2 N13: the signal went out and ``stop_sent_epoch`` followed,
    without the lock the script takes: a script run between the two double-fired."""
    at_forward(m)
    m.child.isig(False)  # (the pty would not turn ^C into a signal: the pid route)
    seen = []

    def spy(pid, sig):
        on_disk = json.loads((m.folder / "record.json").read_text())
        seen.append((on_disk.get("stop_sent_epoch"), lock_is_held_by_the_page(m)))
        m.signals.append(("kill", pid, sig))

    m.host.kill = spy
    assert m.stop()["via"] == "pid"
    assert len(seen) == 1
    assert seen[0][0] is not None and seen[0][0] > 0, "the record said sent first"
    assert seen[0][1] is True, "and the lock was held while the signal went"
    assert lock_is_held_by_the_page(m) is False, "and it is let go after"


def test_a_signal_that_cannot_be_sent_puts_the_record_back(m):
    at_forward(m)
    m.child.isig(False)
    m.host.tcgetpgrp = lambda fd: (_ for _ in ()).throw(OSError(25, "not a tty"))

    def refuse(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    m.host.kill = refuse
    assert refusal(m.sup.stop, stop_body(m))[1] == "signal_failed"
    assert m.record()["stop_sent_epoch"] is None, "on disk"
    assert m.sup._rec["stop_sent_epoch"] is None, "in memory"
    doc = m.doc()
    assert doc["stop"]["allowed"] is True and doc["stop"]["phase"] == "idle"
    m.host.kill = lambda pid, sig: m.signals.append(("kill", pid, sig))
    assert m.stop()["via"] == "pid", "and Stop can still be sent"


def test_a_retry_that_cannot_be_sent_does_not_use_the_retry_up(m, monkeypatch):
    monkeypatch.setattr(st, "STOP_ACK_S", 0.2)
    at_forward(m)
    m.stop()
    first = m.record()["stop_sent_epoch"]
    time.sleep(0.4)
    m.look()

    def refuse(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    m.host.kill = refuse
    m.host.tcgetpgrp = lambda fd: (_ for _ in ()).throw(OSError(25, "not a tty"))
    assert refusal(m.sup.stop, stop_body(m))[1] == "signal_failed"
    rec = m.record()
    assert rec["stop_sent_epoch"] == first and not rec.get("stop_retried")
    assert rec.get("stop_retry_epoch") is None
    assert m.doc()["stop"]["allowed"] is True, "the one retry is still there"


def test_a_stop_the_script_sent_while_the_page_decided_is_not_sent_twice(m):
    """The window the review could not trigger: the page has looked (nothing
    sent), the script writes its record and signal, then the page's Stop goes on.
    Under the lock the page reads again and finds it."""
    at_forward(m)
    real = m.sup._current

    def looked_then_the_script_ran():
        out = real()
        stop_from_elsewhere(m)
        return out

    m.sup._current = looked_then_the_script_ran
    assert refusal(m.sup.stop, stop_body(m))[1] == "stop_spent"
    assert m.typed() == b"", "no second Ctrl+C"
    assert not [s for s in m.signals if s[2] == signal.SIGINT]


def test_a_stop_does_not_wait_for_a_record_lock_someone_else_holds(m, monkeypatch):
    """A Ctrl+C never waits: the lock is tried for a moment, then the signal goes
    out without the disk."""
    monkeypatch.setattr(aeri_run, "RECORD_LOCK_WAIT_S", 0.2)
    at_forward(m)
    held = os.open(m.folder / "record.json.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        answer = m.stop()
        waited = time.monotonic() - started
    finally:
        os.close(held)
    assert answer["accepted"] is True and m.typed() == b"\x03"
    assert waited < 3.0
    assert m.sup._rec["stop_sent_epoch"] is not None, "the page knows in memory"


def test_two_stops_at_once_send_one_and_the_lock_does_not_deadlock(m):
    at_forward(m)
    results = []

    def go():
        try:
            results.append(("ok", m.sup.stop({"request_id": m.rid()})["via"]))
        except st.Refuse as exc:
            results.append(("refused", exc.code))

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert sorted(r[0] for r in results) == ["ok"] + ["refused"] * 5
    assert {r[1] for r in results if r[0] == "refused"} == {"stop_spent"}
    assert m.typed() == b"\x03"
