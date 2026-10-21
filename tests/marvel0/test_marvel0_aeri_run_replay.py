"""The real run of 18 Oct (r08) played through the **supervisor** (the readers, the
heartbeat, the monitor, the pty's mode, the clock), not only through ``derive``:
at each moment of the recording (``test_marvel0_aeri_state_replay.TIMELINE``) the
scripted terminal D shows what r08 showed, the clock then runs on while D shows
nothing new, and the document must keep saying what D is doing. In particular D
waiting for the VLM in the reverse loop (SCENE_ASSESS, r08 seq 49 and 71) is
``busy`` after 25 s, 100 s and 560 s, with ``stop`` offered and the arm not
claimed to move; it was ``unknown`` (``source_conflict``, "the arm may move",
an abort condition of the staged first use) after 20 s, every round."""

import time

import pytest
from aeri_machine import NAME
from aeri_run_kit import FAST, machine_of
from test_marvel0_aeri_state_replay import ENDING, TIMELINE, screen_of

from levi.marvel0 import aeri_journal as jr
from levi.marvel0 import aeri_state as st


class Clock:
    """The one clock of the supervisor, the heartbeat and the monitor file."""

    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


@pytest.fixture(name="m")
def machine(tmp_path, monkeypatch):
    for key, value in FAST:
        monkeypatch.setattr(st, key, value)
    mm = machine_of(tmp_path, monkeypatch)
    mm.clock = Clock()
    mm.start()  # (at the real time; the clock is the supervisor's from here)
    mm.host.now = mm.clock
    mm.host.monotonic = mm.clock
    yield mm
    mm.close()


def alive(m):
    """The heartbeat, the monitor and the viewing tab, all at the clock's time."""
    import json

    body = {
        "backend": "standalone",
        "identity": {"boot": "b", "start_ticks": "1"},
        "pid": m.child.pid,
        "run_id": NAME,
        "state": "running",
        "updated_wall_ns": int(m.clock.t * 1e9),
    }
    m.run_dir.mkdir(parents=True, exist_ok=True)
    (m.run_dir / jr.RUNNER).write_text(json.dumps(body))
    m.health_path.write_text(
        json.dumps(
            {
                "updated_at_epoch": m.clock.t,
                "red_light": False,
                "hardware_active": True,
                "controller_active": True,
                "robot_mode_name": "Move",
                "monitor_pid": 1234,
            }
        )
    )
    m.present()


def play(m, n, k):
    m.script(journal=n, screen=screen_of(k), runner=False, cbreak=True, steps=9)
    alive(m)


def wait(m, seconds):
    """D shows nothing new for ``seconds``; looked at every few seconds."""
    seen = []
    left = seconds
    while left > 0:
        step = min(left, 5.0)
        m.clock.t += step
        left -= step
        alive(m)
        seen.append(m.look())
    return seen


def test_the_whole_real_run_never_turns_unknown_while_d_waits(m):
    checked = 0
    for n, k, _text, state, long in [*TIMELINE, *ENDING]:
        if state == "stopping_or_busy":
            continue
        play(m, n, k)
        looks = [m.look()]
        # the clock runs on; for a D that really waits (a person's key, the VLM)
        # as long as the longest external answer plus a margin
        looks += wait(m, 600.0 if long else 8.0)
        for doc in looks:
            assert doc["state"]["id"] != "unknown", (n, k, doc["state"])
            assert doc["state"]["reason"] not in (
                "source_conflict",
                "screen_drift",
                "runner_stale",
            ), (n, k, doc["state"])
            checked += 1
        assert looks[-1]["state"]["id"] == state or not long, (n, k, state, looks[-1]["state"])
        assert looks[-1]["d"]["alive"] is True
    assert checked > 100


def test_d_waiting_for_the_vlm_in_the_reverse_loop_can_be_stopped_all_along(m):
    play(m, 50, 69)  # r08 seq 49: SCENE_ASSESS (subtask_forward_done)
    for doc in [m.look(), *wait(m, 560.0)]:
        assert doc["state"]["id"] == "busy" and doc["state"]["reason"] == "planning"
        assert doc["state"]["may_move"] is False and doc["keys"] == []
        assert doc["stop"]["allowed"] is True and doc["stop"]["phase"] == "idle"
        assert doc["agree"]["screen"] == "ok" and doc["agree"]["journal"] == "ok"
    assert m.stop()["via"] == "pty"
    assert m.typed() == b"\x03"


def test_the_second_wait_after_a_retry_is_the_same(m):
    play(m, 72, 75)  # r08 seq 71: SCENE_ASSESS (subtask_continue)
    for doc in [m.look(), *wait(m, 100.0)]:
        assert doc["state"]["id"] == "busy" and doc["stop"]["allowed"] is True


def test_an_external_answer_that_takes_minutes_is_shown_as_what_it_is(m):
    play(m, 50, 69)
    prompt = m.runs / NAME / "queue" / "000001-plan"
    prompt.mkdir(parents=True)
    (prompt / "prompt.txt").write_text("plan the reverse loop")
    docs = wait(m, 300.0)
    assert all(
        d["state"]["id"] == "busy" and d["state"]["reason"] == "waiting_agent"
        for d in docs
    )
    assert docs[-1]["queue"]["pending"] == 1 and docs[-1]["queue"]["oldest_age_s"] > 200
