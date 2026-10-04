"""The supervisor judges time on one clock: the ``now`` its tick is given.

A worker's start and a person's wait were once stamped with ``time.time()``
while the tick compared them with its own ``now``. With the two clocks apart
(a test's simulated ticks, a loaded machine between reading the clock and
using it, a wall clock that jumped) a running worker was stopped as stalled at
once, or a person's approval was never looked at again. Here the wall clock
is made to run a day behind the tick's clock: nothing may notice."""

import time

from test_live_dataset_names import (
    BASE,
    config_for,
    no_model_server_anywhere,
    rollouts,
    supervisor,
)

from levi.live import controller, jsonio

DAY = 86400.0


class Running:
    """A worker process that keeps running until it is signalled."""

    pid = 4243

    def __init__(self):
        self.code = None

    def poll(self):
        return self.code

    @property
    def returncode(self):
        return self.code

    def wait(self):
        return self.code


def wall_clock_a_day_behind(monkeypatch):
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - DAY)


def setup(tmp_path, demo_template, monkeypatch):
    no_model_server_anywhere(monkeypatch)
    r = rollouts(tmp_path, demo_template, "root")
    r.write(0)
    c = config_for(tmp_path, [r.root])
    ctl, made, messages = supervisor(c)
    return ctl, made, messages


def test_a_running_worker_is_not_taken_for_stalled_when_the_clocks_differ(
    tmp_path, demo_template, monkeypatch
):
    ctl, _made, messages = setup(tmp_path, demo_template, monkeypatch)
    proc = Running()
    ctl.popen = lambda *a, **k: proc
    signals = []

    def killpg(pid, sig):
        signals.append((pid, sig))
        proc.code = -sig

    monkeypatch.setattr(controller.os, "killpg", killpg)
    t = time.time()  # the tick's clock: today
    wall_clock_a_day_behind(monkeypatch)
    ctl.tick(t)
    assert ctl.worker is proc
    # The worker said who it is but has not reported progress yet: a stall is
    # measured from its start.
    jsonio.write(ctl.config.live_dir / "worker.json", {"pid": proc.pid})
    for i in range(1, 20):  # ten seconds of ticks, far from WORKER_STALL_S
        ctl.tick(t + i * 0.5)
    assert ctl.worker is proc and not signals, messages
    assert not any("stalled" in m for m in messages)
    # A worker that really is silent for longer is still stopped.
    ctl.tick(t + controller.WORKER_STALL_S + 5)
    assert signals == [(proc.pid, controller.signal.SIGTERM)]
    assert any("stalled" in m for m in messages)


def test_a_person_who_acted_is_noticed_when_the_clocks_differ(
    tmp_path, demo_template, monkeypatch
):
    ctl, _made, _messages = setup(tmp_path, demo_template, monkeypatch)
    t = time.time()
    wall_clock_a_day_behind(monkeypatch)
    ctl.tick(t)  # scans; the dataset has work
    assert BASE in ctl._queue(t)
    asked = []

    def acted(ws, info):
        asked.append(info)
        return True

    ctl._acted = acted
    ctl.worker, ctl.worker_dataset = None, BASE
    ctl._reaped(11, now=t)  # the worker waits for a person
    assert BASE in ctl.awaiting
    recheck = ctl.config.pipeline.human_recheck_s
    assert BASE not in ctl._queue(t + 1) and not asked  # too soon to look
    assert BASE in ctl._queue(t + recheck + 1)
    assert asked and BASE not in ctl.awaiting
