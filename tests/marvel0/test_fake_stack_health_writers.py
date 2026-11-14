"""The stand-ins A and C write one fake health file; neither may lose the other's
flag (a lost ``controller_active`` left C "starting" until its ready timeout, so
the chain test failed with ``c_failed`` on a loaded runner)."""

import json
import threading

from conftest import scaled
from fake_stack import fake_common


def test_a_write_between_anothers_read_and_replace_is_not_lost(tmp_path, monkeypatch):
    health = str(tmp_path / "health.json")
    fake_common.write_health(health, hardware_active=True, controller_active=False)
    real_load = fake_common.json.load
    interlopers = []

    def load_then_let_c_write(handle):
        data = real_load(handle)
        # (once: A has read the file, and now C writes its flag, on its own
        # thread: the interleaving of two programs that a loaded machine makes)
        monkeypatch.setattr(fake_common.json, "load", real_load)
        c = threading.Thread(
            target=fake_common.write_health,
            args=(health,),
            kwargs={"controller_active": True},
            daemon=True,
        )
        c.start()
        c.join(scaled(0.3))  # (done, or waiting for A's lock: both are fine)
        interlopers.append(c)
        return data

    monkeypatch.setattr(fake_common.json, "load", load_then_let_c_write)
    fake_common.write_health(health, hardware_active=True)  # A's heartbeat
    interlopers[0].join(scaled(5))
    assert not interlopers[0].is_alive()
    with open(health) as handle:
        got = json.load(handle)
    assert (got["hardware_active"], got["controller_active"]) == (True, True), got
