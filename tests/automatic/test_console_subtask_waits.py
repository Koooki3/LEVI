"""Terminal D's WAIT_HUMAN lines of a subtask_loop run (console.py): each
subtask reason prints its own sentence with the concrete cause from its note,
a line saying in which loop the run stopped and whether the reverse reset
ran, and the VLM/memory waits and the retry limit need a second ``y``.
Fake keys and fakes only."""

from types import SimpleNamespace as NS

import pytest
from console_keys import DeadlineKeys as Keys
from subtask_rig import FORWARD, build
from test_console import Out

from levi.automatic import console as C
from levi.automatic import console_text as T
from levi.automatic import subtask_loop as st

EN = T.Texts("en")
ZH = T.Texts("zh")


def waits(over):
    return {
        "subtask_plan_refused": {"plans": ["{"]},
        "subtask_check_unavailable": {"checks": [st.check_answer(None)]},
        "subtask_retry_limit": {"checks": st.checks(*[False] * 8)},
        "subtask_inventory_rejected": {"gate": st.ScriptedGate(inventory=("no",))},
        "subtask_not_released": {"gate": st.ScriptedGate(releases=["refused"])},
        "vlm_wake_refused": {"power": st.FakeVlmPower(free_mib=20000, awake=False)},
        "vlm_wake_failed": {"power": st.FakeVlmPower(wake_ok=False, awake=False)},
        "vlm_sleep_refused": {"power": st.FakeVlmPower(mem_gib=20.0)},
    }[over]


# (wait, the sentence's start in English, needs y, the phase line's key)
CASES = [
    (
        "subtask_plan_refused",
        "The VLM's forward plan was refused (bad_json)",
        False,
        "sub_wait_idle",
    ),
    (
        "subtask_check_unavailable",
        "The VLM could not tell whether forward subtask #0 is done",
        False,
        "sub_wait_forward",
    ),
    (
        "subtask_retry_limit",
        "forward subtask #0 failed its check 6 times (retry limit 5)",
        True,
        "sub_wait_forward",
    ),
    (
        "subtask_inventory_rejected",
        "The object list was rejected (terminal:",
        False,
        "sub_wait_idle",
    ),
    (
        "subtask_not_released",
        "This round was not released at terminal D",
        False,
        "sub_wait_idle",
    ),
    (
        "vlm_wake_refused",
        "free GPU memory 20000 MiB < threshold 24024 MiB",
        True,
        "sub_wait_idle",
    ),
    (
        "vlm_wake_failed",
        "The VLM did not wake up (wake_failed_or_timeout)",
        True,
        "sub_wait_idle",
    ),
    (
        "vlm_sleep_refused",
        "The VLM was not put to sleep (host_memory_short; MemAvailable 20.0 GiB",
        True,
        "sub_wait_idle",
    ),
]


def waiting(tmp_path, wait, script):
    r = build(tmp_path, **waits(wait))
    assert r.orch.run() == "WAIT_HUMAN"
    out = Out()
    keys = Keys(out, script)
    console = C.Console(keys, out, lang="en")
    console.bind(r.orch, NS(stopped=lambda: False, health=lambda: {"state": "ok"}))
    return r, console, out


@pytest.mark.parametrize("wait, text, confirm, phase", CASES)
def test_each_subtask_wait_says_what_happened_and_where_it_stopped(
    tmp_path, wait, text, confirm, phase
):
    """Enter, then q at the second confirmation if one is asked: the line,
    the phase line and the confirmation rule (m35: Enter alone never resumes
    a confirming wait)."""
    r, console, out = waiting(
        tmp_path, wait, [("(q ends)", "\n"), ("second confirmation", "q")]
    )
    resumed = console.wait_human()
    screen = out.getvalue()
    assert text in screen, screen
    assert EN(phase, n=r.orch.sub.round_no) in screen
    if confirm:
        assert "second confirmation" in screen
        assert resumed is False and r.orch.state == "COMPLETED"  # q ended it
    else:
        assert "second confirmation" not in screen
        assert resumed is True and r.orch.state == "PREFLIGHT"


@pytest.mark.parametrize("wait", [c[0] for c in CASES if c[2]])
def test_a_confirming_wait_resumes_only_with_y(tmp_path, wait):
    """m35: Enter then y resumes; Enter then Enter (or any other key) does not."""
    r, console, _out = waiting(
        tmp_path, wait, [("(q ends)", "\n"), ("second confirmation", "\n\nxy")]
    )
    assert console.wait_human() is True
    assert r.orch.state == "PREFLIGHT"
    seen = [k for kind, k in console.keys.log if kind == "key"]
    assert seen[-1] == "y"


def test_a_stop_during_the_reverse_loop_says_the_scene_is_partly_restored(tmp_path):
    holder = []

    def on_call(kind, info):
        if (
            kind == "check"
            and len([c for c in holder[0].vlm.calls if c[0] == "check"]) == 3
        ):
            holder[0].orch.stop("stop-r")

    r = build(tmp_path, on_call=on_call)
    holder.append(r)
    assert r.orch.run() == "WAIT_HUMAN"
    out = Out()
    console = C.Console(
        Keys(out, [("(then y to confirm)", "\n"), ("second confirmation", "q")]),
        out,
        lang="en",
    )
    console.bind(r.orch, NS(stopped=lambda: False, health=lambda: {"state": "ok"}))
    console.wait_human()
    assert EN("sub_wait_reverse", n=1) in out.getvalue()
    assert "partly restored" in out.getvalue()


def test_a_wait_after_the_forward_loop_says_the_reverse_did_not_run(tmp_path):
    r = build(tmp_path, plans=[st.plan_answer(FORWARD), "{"])
    assert r.orch.run() == "WAIT_HUMAN"
    out = Out()
    console = C.Console(Keys(out, [("(q ends)", "q")]), out, lang="en")
    console.bind(r.orch, NS(stopped=lambda: False, health=lambda: {"state": "ok"}))
    console.wait_human()
    screen = out.getvalue()
    assert "The VLM's reverse plan was refused (bad_json)" in screen
    assert EN("sub_wait_after_forward", n=1) in screen
    assert "the reverse reset did not run" in screen


def test_the_texts_exist_in_both_languages_with_the_same_fields():
    import string

    keys = [
        k
        for k in T.TEXT["en"]
        if k.startswith(
            ("wait_subtask", "wait_vlm", "todo_subtask", "todo_vlm", "sub_wait")
        )
    ]
    assert len(keys) >= 19
    for key in keys:
        fields = lambda text: {f for _, f, _, _ in string.Formatter().parse(text) if f}
        assert fields(T.TEXT["en"][key]) == fields(T.TEXT["zh"][key]), key
        assert any("一" <= c <= "鿿" for c in ZH(key)), key
    assert {"vlm_unavailable", "subtask_retry_limit"} <= C.CONFIRM_REASONS
    assert not {"subtask_plan_refused", "subtask_check_unavailable"} & C.CONFIRM_REASONS
    assert "subtask_check" not in C.DISPLAY_NOTES  # the VLM's answer stays off screen
    assert {
        "vlm_wake_refused",
        "subtask_plan_refused",
        "subtask_retry",
    } <= C.DISPLAY_NOTES


def test_the_recent_notes_lines_show_the_subtask_notes(tmp_path):
    _r, console, out = waiting(
        tmp_path, "vlm_wake_refused", [("(q ends)", "\n"), ("second confirmation", "q")]
    )
    console.wait_human()
    assert "vlm_wake_refused" in out.getvalue()
