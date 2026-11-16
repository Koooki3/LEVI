"""The takeover on a real terminal (a pty): the terminal stays in cbreak for the whole takeover, entered without a
flush, so keys typed during a move are still there at the next prompt, an ``a`` among them is acted on first, and
none of the others runs (re-review 2026-11-16: ``tty.setcbreak`` flushes on entry and canonical mode shows nothing
before an Enter, so a drain outside cbreak reads nothing)."""

import os
import pty
import time
from types import SimpleNamespace as NS

import pytest
from assist_rig import build as build_rig
from subtask_rig import FORWARD as FWD
from subtask_rig import REVERSE, build, committed, sub_config
from test_console import Out
from test_subtask_grasp_assist import (
    BAND,
    ENTRIES,
    AssistPolicy,
    share_gate,
)

from levi.automatic import console as C
from levi.automatic import grasp_assist as ga
from levi.automatic import state_machine as sm
from levi.automatic import subtask_loop as st


@pytest.fixture
def pty_pair():
    master, slave = pty.openpty()
    yield master, slave
    for fd in (master, slave):
        try:
            os.close(fd)
        except OSError:
            pass


def test_stdin_keys_drain_reads_typed_ahead_only_inside_cbreak(pty_pair):
    master, slave = pty_pair
    keys = C.StdinKeys(fd=slave)
    os.write(master, b"ZZa")  # canonical mode: nothing is readable without an Enter
    time.sleep(0.05)
    assert keys.drain() == ""
    with keys.cbreak(flush=False):  # the takeover's way in: no flush
        time.sleep(0.05)
        assert keys.drain() == "ZZa"
        os.write(master, b"ZZa")
        time.sleep(0.05)
        assert keys.drain() == "ZZa"
    # The default way in flushes what is pending (every other prompt).
    os.write(master, b"ZZa")
    time.sleep(0.05)
    with keys.cbreak():
        time.sleep(0.05)
        assert keys.drain() == ""


def bound_pty(tmp_path, master, slave):
    out = Out()
    keys = C.StdinKeys(fd=slave)
    console = C.Console(keys, out, lang="en")
    fence = sm.MotionFence()
    rig = build_rig(fence=fence, band=BAND)
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
    share_gate(r, rig)
    rig.port.stop_probe = lambda: r.orch.stop_requested()
    console.bind(r.orch, NS(stopped=lambda: False, health=lambda: {"state": "ok"}))
    return NS(r=r, console=console, out=out, rig=rig, keys=keys)


def type_when(out, needle, data, master, start=0, timeout=10.0):
    """Write ``data`` to the terminal once the console printed ``needle`` (a thread
    would race the run; the caller polls between steps instead)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        at = out.getvalue().find(needle, start)
        if at >= 0:
            time.sleep(0.1)  # the prompt has entered its wait
            os.write(master, data)
            return at + len(needle)
        time.sleep(0.005)
    raise AssertionError(f"never printed {needle!r}:\n{out.getvalue()[-1500:]}")


def run_in_thread(b):
    import threading

    box = {}

    def go():
        try:
            box["end"] = b.r.orch.run()
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc

    thread = threading.Thread(target=go)
    thread.start()
    return thread, box


def test_keys_typed_during_a_move_are_dropped_and_an_a_among_them_aborts(
    tmp_path, pty_pair
):
    master, slave = pty_pair
    b = bound_pty(tmp_path, master, slave)
    # ZZa lands in the terminal while the first move's sub-steps are going out.
    b.rig.env.on_pose = lambda n: os.write(master, b"ZZa") if n == 2 else None
    thread, box = run_in_thread(b)
    at = type_when(b.out, "Press y if the list is right", b"y", master)
    at = type_when(b.out, "press Enter to release", b"\n", master, at)
    type_when(b.out, "Position x=", b"x", master, at)
    thread.join(20)
    assert not thread.is_alive(), b.out.getvalue()[-1500:]
    assert box.get("end") == "WAIT_HUMAN", box
    assert len(b.rig.env.pose_targets) == ga.SUBSTEPS  # the one move; no Z ran
    assert committed(b.r.orch)[-1][2] == "grasp_assist_aborted"
    assert "discarded and held an a/q" in b.out.getvalue()
    # The terminal is back in canonical mode once the takeover ended.
    import termios

    assert termios.tcgetattr(slave)[3] & termios.ICANON


def test_the_terminal_is_released_when_the_console_closes_mid_takeover(pty_pair):
    import termios

    _, slave = pty_pair
    console = C.Console(C.StdinKeys(fd=slave), Out(), lang="en")
    console._assist_hold_cbreak()
    assert not termios.tcgetattr(slave)[3] & termios.ICANON
    console.close()
    assert termios.tcgetattr(slave)[3] & termios.ICANON
