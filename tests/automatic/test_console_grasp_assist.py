"""Terminal D's keys of the GRASP_ASSIST takeover (console.py): the bytes of ``grasp_assist.KEY_CHARS`` and nothing
else move or hand over; the target is shown before it is sent; the page text keeps "this is not an emergency stop,
keep a hand on the e-stop". Fake keys, fake run; no terminal, no robot."""

from types import SimpleNamespace as NS

import pytest
from assist_rig import build as build_rig
from console_keys import DeadlineKeys as Keys
from subtask_rig import FORWARD as FWD
from subtask_rig import REVERSE, build, committed, sub_config
from test_console import Out
from test_subtask_grasp_assist import BAND, ENTRIES, AssistPolicy

from levi.automatic import console as C
from levi.automatic import console_text as T
from levi.automatic import grasp_assist as ga
from levi.automatic import state_machine as sm
from levi.automatic import subtask_loop as st

POSITION = "Position x="
INVENTORY = "Press y if the list is right"
RELEASE = "press Enter to release"


def bound(tmp_path, script, *, env_kw=None):
    out = Out()
    keys = Keys(out, [(INVENTORY, "y"), (RELEASE, "\n"), *script])
    console = C.Console(keys, out, lang="en")
    fence = sm.MotionFence()
    rig = build_rig(fence=fence, env_kw=env_kw, band=BAND)
    cfg = sub_config(
        subtask_reverse_policy_role="reset",
        subtask_reset_prompts=ENTRIES,
        subtask_grasp_assist={"enabled": True, "holding_band": [BAND]},
    )
    r = build(
        tmp_path,
        cfg=cfg,
        fence=fence,
        gate=console,
        grasp_assist=ga.AssistRig(rig.port, rig.limits),
        plans=[st.plan_answer(FWD), st.plan_answer(REVERSE)],
        checks=st.checks(True, True, True, True),
        policy_factory=lambda clock, trace, power: AssistPolicy(
            clock, trace=trace, power=power
        ),
    )
    r.policy.robot = r.robot
    console.bind(r.orch, NS(stopped=lambda: False, health=lambda: {"state": "ok"}))
    return NS(r=r, console=console, out=out, rig=rig, keys=keys)


def test_the_keys_are_the_table_and_only_the_table():
    assert ga.KEY_CHARS == {
        "x": "x+",
        "X": "x-",
        "y": "y+",
        "Y": "y-",
        "z": "z+",
        "Z": "z-",
        "c": "close",
        "u": "lift",
        "o": "open",
        "g": "handback",
        "a": "abort",
    }
    assert set(ga.KEY_CHARS.values()) == set(ga.KEYS)


def test_close_lift_and_hand_back_by_key_with_the_target_shown_first(tmp_path):
    b = bound(
        tmp_path,
        [(POSITION, "x"), (POSITION, "c"), (POSITION, "u"), (POSITION, "g")],
    )
    assert b.r.orch.run() == "COMPLETED"
    text = b.out.getvalue()
    # The banner: what it is, the keys, the limits, and that it is not a stop.
    assert "GRASP_ASSIST: you take over the grasp" in text
    assert "This is not an emergency stop; keep a hand on the e-stop" in text
    assert "x/X y/Y z/Z = move 10.0 mm" in text and "u lift 20 mm" in text
    assert "at most 20 mm per axis per key, z >= 0.05325 m" in text
    # The target and its checks come before the move's result.
    target = text.index("Target x=0.490")
    assert target < text.index("Done: on target within", target)
    assert "z_floor_ok=True" in text and "travel_ok=True" in text
    # The measured opening and the fact status are shown with each prompt.
    assert "measured opening 0.299" in text and "holding fact holding_verified" in text
    # The hand-back says the policy's state[6] is the command.
    assert "state[6]=0.0 (the command, not the measured opening)" in text
    assert "Handed over to the reset policy" in text and "no env.reset" in text
    assert ("GRASP_ASSIST", "RESET_ACTIVE", "subtask_dispatched") in committed(b.r.orch)


def test_a_key_outside_the_table_does_nothing(tmp_path):
    b = bound(tmp_path, [(POSITION, "kmw+-1 a")])
    assert b.r.orch.run() == "WAIT_HUMAN"
    assert b.rig.env.pose_targets == []
    assert committed(b.r.orch)[-1][2] == "grasp_assist_aborted"
    assert "Takeover abandoned (operator_abort)" in b.out.getvalue()


def test_a_refused_key_says_why_and_sends_nothing(tmp_path):
    b = bound(
        tmp_path,
        [(POSITION, "Z"), (POSITION, "g"), (POSITION, "a")],
        env_kw={"start": (0.48, -0.011, 0.06)},
    )
    assert b.r.orch.run() == "WAIT_HUMAN"
    text = b.out.getvalue()
    assert "Refused (z_below_floor)" in text and "Nothing was sent" in text
    assert "Refused (no_holding_fact)" in text
    assert b.rig.env.pose_targets == []


def test_q_ends_the_run_through_the_takeover_without_a_home(tmp_path):
    b = bound(tmp_path, [(POSITION, "q")])
    assert b.r.orch.run() == "WAIT_HUMAN"
    assert committed(b.r.orch)[-1][2] == "grasp_assist_aborted"
    assert b.console._quit is True
    assert len([t for t in b.r.trace if t == ("home",)]) == 1


def test_the_takeover_notes_are_shown_in_the_recent_notes_of_a_wait():
    for code in ("grasp_assist_step", "grasp_assist_handoff", "grasp_assist_fault"):
        assert code in C.DISPLAY_NOTES


def test_the_wait_after_an_abort_has_its_sentences_in_both_languages():
    happened, todo = C.WAIT_REASONS["grasp_assist_aborted"]
    for lang in T.LANGUAGES:
        t = T.Texts(lang)
        assert t(happened) != happened and t(todo) != todo
    assert "not homed" in T.Texts("en")(happened)
    status = C.status_row(state="GRASP_ASSIST")
    assert status.say == "st_grasp_assist" and status.announce is True
    assert "e-stop" in T.Texts("en")("st_grasp_assist")
    assert "急停" in T.Texts("zh")("st_grasp_assist")


@pytest.mark.parametrize("key", [k for k in T.TEXT["en"] if k.startswith("assist_")])
def test_every_takeover_sentence_has_both_languages_and_the_same_fields(key):
    import string

    def fields(text):
        return {f for _, f, _, _ in string.Formatter().parse(text) if f}

    assert fields(T.TEXT["en"][key]) == fields(T.TEXT["zh"][key])
