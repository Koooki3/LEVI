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
import json
import os
import signal
import stat
import time

import pytest
from aeri_run_kit import (
    at_forward,
    at_release,
    interrupt_line,
    machine_of,
    operator_interrupt_note,
    refusal,
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
