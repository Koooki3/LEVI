"""The wiring check of the start (``Host.hosted_visible``, the very function
``aeri_activity.hosted_visible_to_activity``): ``levi stop`` and the R6 restart
read ``activity.running_jobs()``, and a hosted D that is not in it is taken for no
job at all and cut short by the hang-up chain (SIGINT, 3 s, SIGKILL, the 7/7
shutdown never runs). So a start whose D ``levi stop`` could not list is refused
before the launch, and one that started and is not listed gets Ctrl+C at its
phrase (the arm is not touched before it). The scripted machine tells the answer
(``m.visible``); the tests with real processes ask the real list of jobs."""

import pytest
from aeri_machine import NAME
from aeri_run_kit import machine_of, refusal

from levi.marvel0 import aeri_state as st


@pytest.fixture(name="m")
def machine(tmp_path, monkeypatch):
    mm = machine_of(tmp_path, monkeypatch)
    yield mm
    mm.close()


def test_a_start_whose_d_levi_stop_could_not_list_is_refused(m):
    """The R6 restart reads ``activity.running_jobs()``; a hosted D that is not in
    it is taken for no job and cut short by the hang-up chain (R10 High 3). The
    wiring is checked at the challenge and at the start."""
    m.visible = False
    m.present()
    _status, code, extra = refusal(m.sup.challenge, {"purpose": "start", "name": NAME})
    assert code == "start_failed" and extra["why"] == "not_listed_by_levi_stop"
    assert m.spawned == []
    m.visible = True
    assert m.sup.challenge({"purpose": "start", "name": NAME})["name"] == NAME


def test_a_d_that_started_and_is_not_listed_is_stopped_at_its_phrase(m):
    seen = []

    def listed_before_only(workspace, run_id):
        seen.append(run_id)
        return run_id is None  # the wiring is whole, the real list lacks this run

    m.visible = listed_before_only
    m.present()
    c = m.sup.challenge({"purpose": "start", "name": NAME})
    body = {
        "request_id": m.rid(),
        "name": NAME,
        "challenge": c["challenge"],
        "confirm": f"start aeri {NAME}",
        "attest": {k: True for k in st.ATTEST},
    }
    _status, code, extra = refusal(m.sup.start, body)
    assert code == "start_failed" and extra["why"] == "not_listed_by_levi_stop"
    assert any(run_id for run_id in seen), "the list was asked for this run"
    # Ctrl+C at the phrase (the arm is not touched before it): by the pty, where
    # in the canonical mode the byte waits in the line buffer (the real SIGINT is
    # in the tests with real ptys)
    assert m.record()["stop_sent_epoch"] is not None
    assert any(r["event"] == "stop" and r["route"] == "pty" for r in m.history())
    assert any(r["event"] == "start_unlisted" for r in m.history())
