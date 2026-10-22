"""Deterministic collection races in the process stand-in, with no real signals."""

import signal
import threading

import pytest
from conftest import scaled
from fake_stack import process_host


class Child:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


def proc(pid, name):
    return process_host.Proc(
        Child(pid),
        name,
        ["python", "fake_b.py"],
        {"KillSignal": "SIGINT", "TimeoutStopSec": "4", "SendSIGKILL": "no"},
    )


@pytest.mark.parametrize("replace_unit", [False, True])
def test_stop_preserves_collection_and_replacement_by_show(monkeypatch, replace_unit):
    host = process_host.ProcessHost()
    name = "fake-host-collection-race"
    original = proc(100000001, name)
    replacement = proc(100000002, name)
    host.units[name] = original
    exited = threading.Event()
    release_stop = threading.Event()
    outcomes, errors = [], []

    def signal_only_this_fake(pid, sig):
        assert (pid, sig) == (original.popen.pid, signal.SIGINT)
        original.popen.returncode = 0
        exited.set()
        assert release_stop.wait(scaled(5)), "test did not release the stop worker"

    monkeypatch.setattr(process_host.os, "killpg", signal_only_this_fake)

    def stop():
        try:
            outcomes.append(host._stop(name + ".service", 20))
        except KeyError as exc:
            errors.append(exc)

    worker = threading.Thread(target=stop, name="fake-host-stop-race")
    worker.start()
    try:
        assert exited.wait(scaled(5)), "stop worker did not reach the signal"
        # A ticker reads the cleanly exited child before systemctl stop returns.
        assert "LoadState=not-found" in host._show(name + ".service")
        assert name not in host.units
        if replace_unit:
            host.units[name] = replacement
    finally:
        release_stop.set()
        worker.join(scaled(5))

    assert not worker.is_alive(), "stop worker was not joined"
    assert not errors, f"stop raised after concurrent collection: {errors!r}"
    assert outcomes == [(0, "")]
    assert original.signals == ["SIGINT"]
    if replace_unit:
        assert host.units.get(name) is replacement, "stop removed a new unit instance"
    else:
        assert name not in host.units


def test_show_missing_unit_is_idempotent():
    host = process_host.ProcessHost()
    for _ in range(2):
        assert "LoadState=not-found" in host._show("missing.service")
    assert host.units == {}
