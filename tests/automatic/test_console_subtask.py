"""Terminal D in a subtask_loop run (console.py): only its keys confirm the
object list and release a round; s/f/d during the reverse loop say they do
nothing there. Fake keys, fake run; no terminal, no robot."""

from types import SimpleNamespace as NS

import pytest
from console_keys import DeadlineKeys as Keys
from subtask_rig import build, committed, prompts
from test_console import Out

from levi.automatic import console as C


class Bound(NS):
    """The run, the console, its keys and its screen."""


def bound(tmp_path, script=(), stale="", **over):
    out = Out()
    keys = Keys(out, script, stale)
    console = C.Console(keys, out, lang="en")
    r = build(tmp_path, gate=console, **over)
    console.bind(r.orch, NS(stopped=lambda: False, health=lambda: {"state": "ok"}))
    return Bound(r=r, console=console, keys=keys, out=out)


def test_y_confirms_the_list_and_enter_releases_the_round(tmp_path):
    b = bound(
        tmp_path,
        [("Press y if the list is right", "y"), ("press Enter to release", "\n")],
    )
    r, console, keys, out = b.r, b.console, b.keys, b.out  # noqa: F841
    assert r.orch.run() == "COMPLETED"
    text = out.getvalue()
    assert "pink_plate_1: pink plate, on table, graspable" in text
    assert "Round 1/1 released" in text
    assert len(prompts(r)) == 4


def test_n_rejects_the_list(tmp_path):
    b = bound(tmp_path, [("Press y if the list is right", "n")])
    r, console, keys, out = b.r, b.console, b.keys, b.out  # noqa: F841
    assert r.orch.run() == "WAIT_HUMAN"
    assert committed(r.orch)[-1][2] == "subtask_inventory_rejected"
    assert r.policy.acquired == []


def test_a_stale_enter_never_releases_a_round(tmp_path):
    """R1: keys typed before the prompt are flushed; q then ends the run."""
    b = bound(
        tmp_path,
        [("Press y if the list is right", "y"), ("press Enter to release", "q")],
        stale="\n\n",
    )
    r, console, keys, out = b.r, b.console, b.keys, b.out  # noqa: F841
    assert r.orch.run() == "WAIT_HUMAN"
    assert committed(r.orch)[-1][2] == "operator_stop"
    assert r.policy.acquired == []
    assert "released" not in out.getvalue().split("press Enter to release")[-1]


def test_q_at_the_list_stops_the_run(tmp_path):
    b = bound(tmp_path, [("Press y if the list is right", "q")])
    r, console, keys, out = b.r, b.console, b.keys, b.out  # noqa: F841
    assert r.orch.run() == "WAIT_HUMAN"
    assert committed(r.orch)[-1][2] == "operator_stop"


def test_s_during_the_reverse_loop_does_nothing_and_says_so(tmp_path):
    """R9 refinement / m19 (console side): the key is read, a line says it
    does nothing here, and no verdict is sent."""
    b = bound(
        tmp_path,
        [("Press y if the list is right", "y"), ("press Enter to release", "\n")],
    )
    r, console, keys, out = b.r, b.console, b.keys, b.out
    sent = []
    original_verdict = r.orch.episode_verdict

    def verdict(*a, **k):
        sent.append(a)
        return original_verdict(*a, **k)

    r.orch.episode_verdict = verdict
    r.orch.listener = console.on_state
    step = r.robot.step

    def step_(action, *, token):
        ctx = r.orch.ctx
        if ctx is not None and ctx.role == "reset" and ctx.steps_done == 3:
            keys.buffer.extend("s")
            console.poll_verdict()
        return step(action, token=token)

    r.robot.step = step_
    assert r.orch.run() == "COMPLETED"
    assert sent == []
    assert out.getvalue().count("The reverse loop is restoring the scene") == 2
    assert r.robot.step_calls == 4 * 12


def test_the_forward_hint_names_the_subtask_meaning_of_s_f_d(tmp_path):
    b = bound(
        tmp_path,
        [("Press y if the list is right", "y"), ("press Enter to release", "\n")],
    )
    r, console, keys, out = b.r, b.console, b.keys, b.out  # noqa: F841
    r.orch.listener = console.on_state
    r.orch.run()
    assert "the reverse loop still restores" in out.getvalue()


def test_the_texts_exist_in_both_languages():
    from levi.automatic import console_text as T

    for key in ("sub_release", "sub_reverse_ignored", "sub_inv_todo"):
        assert key in T.TEXT["zh"] and key in T.TEXT["en"]
    assert "不起作用" in T.TEXT["zh"]["sub_reverse_ignored"]


def test_startup_home_has_three_distinct_confirmations_not_enter_or_resume(
    tmp_path, monkeypatch
):
    b = bound(tmp_path)
    keys = iter(("o", "p", "h"))
    flushes = []
    monkeypatch.setattr(b.keys, "flush", lambda: flushes.append(True))

    def wait(handle, **kwargs):
        assert handle("\n") is None
        assert handle("y") is None
        return handle(next(keys))

    monkeypatch.setattr(b.console, "_wait_key", wait)
    assert b.console.confirm_start_home() == {
        "object_handled": True,
        "path_clear": True,
        "gripper_will_open": True,
    }
    assert len(flushes) == 3
    for text in ("opens the gripper", "held object", "return path", "separate Enter"):
        assert text in b.out.getvalue()


def test_startup_home_real_key_loop_flushes_each_stage_and_ignores_enter(tmp_path):
    b = bound(
        tmp_path,
        [
            ("then press o", "\nyoph"),
            ("then press p", "\nyph"),
            ("authorize this home with h", "\nyh"),
        ],
        stale="oph\ny",
    )
    # Real _wait_key and real key-buffer flush: p/h typed with o, and h typed
    # with p, must be discarded rather than approving the next prompt.
    assert b.console.confirm_start_home() == {
        "object_handled": True,
        "path_clear": True,
        "gripper_will_open": True,
    }
    assert not b.keys.script
    assert b.keys.flushes == 3
    assert [n for kind, n in b.keys.log if kind == "flush"] == [5, 2, 1]
    assert [key for kind, key in b.keys.log if kind == "key"] == list("\nyo\nyp\nyh")


def test_stale_startup_confirmation_cannot_approve_any_stage(tmp_path):
    b = bound(tmp_path, [("then press o", "q")], stale="oph\n")
    assert b.console.confirm_start_home() is None
    assert [key for kind, key in b.keys.log if kind == "key"] == ["q"]
    assert "entire return path" not in b.out.getvalue()


@pytest.mark.parametrize("at", ["RESET_VERIFY", "RESET_FINALIZE"])
def test_m_is_scoped_through_reverse_verification_and_other_keys_are_not_requests(
    tmp_path,
    at,
):
    b = bound(
        tmp_path,
        [("Press y if the list is right", "y"), ("press Enter to release", "\n")],
    )
    orch = b.r.orch
    calls = []
    original = orch.reverse_human_request

    def record(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    orch.reverse_human_request = record

    def listener(state, info):
        b.console.on_state(state, info)
        if state == "RESET_ACTIVE":
            for key in "\nsfd":
                b.keys.buffer.extend(key)
                b.console.poll_verdict()
            assert calls == []
        if state == at:
            b.keys.buffer.extend("m")
            b.console.poll_verdict()

    orch.listener = listener
    assert orch.run() == "WAIT_HUMAN"
    assert len(calls) == 1
    assert calls[0]["episode_id"].endswith("reset.0001")
    assert "does not authorize approaching" in b.out.getvalue()
    assert b.console._verdict_episode is None


def test_backend_closed_window_flushes_m_before_delayed_state_notification(tmp_path):
    b = bound(
        tmp_path,
        [("Press y if the list is right", "y"), ("press Enter to release", "\n")],
    )
    orch = b.r.orch
    original_next = orch._next_subtask
    checked = []
    requests = []
    original_request = orch.reverse_human_request

    def request(*args, **kwargs):
        requests.append(args)
        return original_request(*args, **kwargs)

    orch.reverse_human_request = request

    def next_subtask(ctx, value, **kwargs):
        if ctx.role == "reset":
            assert orch.state == "RESET_FINALIZE"
            assert orch.reverse_human_scope() is None
            assert b.console._verdict_episode == ctx.episode_id
            b.keys.buffer.extend("m")
            b.console.poll_verdict()
            assert not b.keys.buffer
            assert b.console._verdict_episode is None
            assert b.console._verdict_cbreak is None
            checked.append(ctx.episode_id)
        return original_next(ctx, value, **kwargs)

    orch._next_subtask = next_subtask

    def listener(state, info):
        b.console.on_state(state, info)
        if state == "RESET_ACTIVE" and checked:
            b.console.poll_verdict()  # old m was flushed, not next-episode input
            assert requests == []

    orch.listener = listener
    assert orch.run() == "COMPLETED"
    assert len(checked) == 2
    assert requests == []


def test_an_early_check_says_what_triggered_it_never_the_answer(tmp_path):
    from aeri_harness import RUN

    from levi.automatic import subtask_loop as st

    b = bound(
        tmp_path,
        [("Press y if the list is right", "y"), ("press Enter to release", "\n")],
        subtask_max_steps=30,
        subtask_early_check=st.EarlyCheck(
            on_events=True, settle_steps=2, min_steps=3, cooldown_steps=5
        ),
        events={f"{RUN}.forward.0001": [{"step": 4, "event_type": "gripper_open"}]},
        checks=st.checks(False, True, True, True, True),
    )
    r, out = b.r, b.out
    r.orch.listener = b.console.on_state
    assert r.orch.run() == "COMPLETED"
    text = out.getvalue()
    assert "paused at step 6/30 (event gripper_open)" in text
    assert "the VLM checks whether it is done" in text
    # The VLM's own answer (false here) is never printed.
    assert '"value"' not in text


def test_the_summary_names_the_early_check_only_when_on():
    from levi.automatic import console_text as T

    assert "{events}" in T.TEXT["zh"]["sum_early_check"]
    assert "{events}" in T.TEXT["en"]["sum_early_check"]
    for key in ("subtask_early_shown", "sub_early_event", "sub_early_interval"):
        assert key in T.TEXT["zh"] and key in T.TEXT["en"]
