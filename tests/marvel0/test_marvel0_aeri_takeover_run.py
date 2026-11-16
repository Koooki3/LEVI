"""The takeover keys through the supervisor on the scripted machine
(``aeri_machine.py``): the bytes the page writes to terminal D's keyboard, one
click, one byte, one key per waiting line. The test reads **every byte** the
page wrote to the (fake) keyboard. Nothing is started and nothing signalled."""

import dataclasses

import pytest
from aeri_machine import NAME, AeriMachine, cut
from test_marvel0_aeri_run import refusal
from test_marvel0_aeri_takeover import ENTER_ASSIST, LETTERS, assist_lines

from levi.marvel0 import aeri_state as st

FAST = (("SETTLE_S", 0.15), ("ARM_DELAY_S", 0.15), ("KEY_GAP_S", 0.3))
MAIN = st.PROFILES["main-ab38c7b1"]


def screen(**kw) -> bytes:
    return cut("Home verified") + assist_lines(**kw).encode()


def machine(tmp_path, monkeypatch, capable=True):
    for key, value in FAST:
        monkeypatch.setattr(st, key, value)
    m = AeriMachine(tmp_path, monkeypatch)
    # the machine's profile has the harvest words on fake sources; the takeover
    # words and capability are the newer profile's, the sources stay the fake ones
    fake = dataclasses.replace(
        m.profile,
        start_home=MAIN.start_home,
        progress_line=MAIN.progress_line,
        camera_stale=MAIN.camera_stale,
        early_check=MAIN.early_check,
        reset_policy=MAIN.reset_policy,
        grasp_assist=capable,
        data={**MAIN.data, "sources_sha256": m.profile.data["sources_sha256"]},
    )
    monkeypatch.setitem(st.PROFILES, "test-profile", fake)
    m.prepare(rounds=5, steps=200)
    m.start()
    m.script(journal=21, screen=screen(), cbreak=True, steps=9)
    m.append_journal([ENTER_ASSIST])
    return m


@pytest.fixture
def m(tmp_path, monkeypatch):
    mm = machine(tmp_path, monkeypatch)
    yield mm
    mm.close()


def test_each_button_writes_exactly_its_byte_once(m):
    seen = b""
    for n, (action, byte) in enumerate(LETTERS.items()):
        answer = m.press(action)
        assert answer["accepted"] is True and answer["byte"] == byte.decode()
        # (``typed`` reads what the keyboard got since the last look)
        got = m.typed()
        assert got == byte, action
        seen += got
        # D answers with a line and prints its waiting line again (a new prompt)
        m.append_screen(assist_lines(steps=4 + n).encode())
        m.wait_state("await_assist")
    assert seen == b"".join(LETTERS.values())


def test_a_second_key_for_the_same_waiting_line_is_refused_and_writes_nothing(m):
    view = m.wait_key("assist_x_pos")
    body = m.body_for(view, "assist_x_pos")
    m.present()
    m.sup.key(body)
    other = m.body_for(view, "assist_z_pos", seen=body["seen"])
    _status, code, _ = refusal(m.sup.key, other)
    assert code in ("key_sent", "stale_page", "state_not_allowed")
    assert m.typed() == b"x"
    # the same request again is the same answer, not another byte
    again = m.sup.key(body)
    assert again["accepted"] is True and m.typed() == b""


def test_hand_back_is_refused_by_the_server_when_the_fact_is_not_a_holding_one(
    tmp_path, monkeypatch
):
    # a machine whose waiting line shows an empty gripper
    m = machine(tmp_path, monkeypatch)
    try:
        m.script(screen=screen(fact="empty"))
        view = m.wait_key("assist_x_pos")
        entry = next(k for k in view["keys"] if k["id"] == "assist_handback")
        assert entry["allowed"] is False
        assert entry["why_not"]["code"] == "assist_no_holding"
        body = m.body_for(view, "assist_x_pos")
        body.update(action="assist_handback", token=entry["token"] or "none")
        m.present()
        _status, code, _ = refusal(m.sup.key, body)
        assert code == "assist_no_holding" and m.typed() == b""
    finally:
        m.close()


def test_a_machine_whose_profile_has_no_takeover_writes_no_takeover_byte(
    tmp_path, monkeypatch
):
    m = machine(tmp_path, monkeypatch, capable=False)
    try:
        m.script(screen=screen())
        view = m.look()
        assert not any(k["id"].startswith("assist_") for k in view["keys"])
        body = {
            "request_id": m.rid(),
            "action": "assist_x_pos",
            "seen": {
                "run_id": view["run_id"],
                "state_seq": view["state"]["seq"],
                "prompt_id": (view["state"]["prompt"] or {}).get("id", "none"),
            },
            "token": "t-none",
            "tab_id": m.tab,
            "confirm": "",
            "attest": [],
        }
        m.present()
        with pytest.raises(st.Refuse):
            m.sup.key(body)
        assert m.typed() == b""
    finally:
        m.close()


def test_the_takeover_keys_are_not_offered_when_the_journal_has_left_the_takeover(m):
    m.wait_key("assist_x_pos")
    m.append_journal(
        [
            {
                "sequence_no": 40,
                "record": "committed",
                "from_state": "GRASP_ASSIST",
                "to_state": "RESET_ACTIVE",
                "reason": "subtask_dispatched",
            }
        ]
    )
    view = m.wait_until(lambda v: v["state"]["id"] != "await_assist", what="left")
    assert not any(k["id"].startswith("assist_") for k in view["keys"])
    assert view["takeover"] is None
    assert NAME
