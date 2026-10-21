"""The way out does not depend on the way in (RIG-04): a stop-all or a cancel that the
backend refuses or cannot take the lock for must not leave a guided start going on to
start B and C. Found by the independent review of the chain (21 Oct)."""

import pytest
from stack_act_machine import ActMachine
from test_marvel0_deploy import forbid_real_machine  # noqa: F401
from test_marvel0_stack_act_chain import to_step

from levi.marvel0 import stack, stack_act_api


@pytest.fixture
def m(tmp_path):
    return ActMachine(tmp_path)


def _settle(m, n=40):
    for _ in range(n):
        m.act.advance()
        m.t += 3


def test_a_refused_stop_all_still_ends_the_chain(m):
    to_step(m, "b_probe")
    folder = m.state_root / "c"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "record.json").write_text("{not json")  # C's record is corrupt
    with pytest.raises(stack.Refuse):
        m.act.sequence_stop(m.stop_all_body("stop-all-first-1"))
    seq = m.doc()["sequence"]
    assert seq["state"] != "running", seq
    runs_before = len(m.systemd_runs())
    _settle(m)
    assert len(m.systemd_runs()) == runs_before, "the chain started something"


def test_a_cancel_ends_the_chain_even_when_the_lock_is_taken(m, monkeypatch):
    to_step(m, "b_probe")

    def busy(*args, **kwargs):
        raise stack.Refuse("stack_busy")

    monkeypatch.setattr(m.act, "_write", busy)  # a chain step holds the machine lock
    with pytest.raises(stack.Refuse):
        m.act.sequence_cancel({"request_id": "cancel-first-0001"})
    assert m.doc()["sequence"]["state"] != "running"
    runs_before = len(m.systemd_runs())
    _settle(m)
    assert len(m.systemd_runs()) == runs_before


def test_the_pauses_go_together():
    stack_act_api.SequenceStartBody(
        request_id="req-00000001",
        confirm="start arm stack",
        pause_before_b=True,
        pause_before_c=True,
    )
    stack_act_api.SequenceStartBody(
        request_id="req-00000002", confirm="start arm stack", pause_before_c=True
    )
    with pytest.raises(ValueError):
        stack_act_api.SequenceStartBody(
            request_id="req-00000003", confirm="start arm stack", pause_before_b=True
        )
