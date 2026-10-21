"""Shared set-up of the tests of the hosted terminal D that are about the findings
of the two reviews of the first build (a Stop sent from elsewhere, the site file
changing in the middle of a run, the start gate, adopting a run): the scripted
machine of ``aeri_machine.py`` at the moments of the real run of 18 Oct, and the
out-of-page stop script loaded as the module it is."""

import importlib.util
import time
from pathlib import Path

import pytest
from aeri_machine import AeriMachine

from levi.marvel0 import aeri_screen as sc
from levi.marvel0 import aeri_state as st
from levi.marvel0.aeri_state import Refuse

FAST = (("SETTLE_S", 0.15), ("ARM_DELAY_S", 0.15), ("KEY_GAP_S", 0.3))
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "aeri_page_stop.py"


def machine_of(tmp_path, monkeypatch, **kw) -> AeriMachine:
    for key, value in FAST:
        monkeypatch.setattr(st, key, value)
    mm = AeriMachine(tmp_path, monkeypatch, **kw)
    mm.prepare(rounds=5, steps=200)
    return mm


def refusal(call, *args, **kw):
    """The ``Refuse`` a call raises, as ``(status, code, extra)``."""
    with pytest.raises(Refuse) as caught:
        call(*args, **kw)
    exc = caught.value
    return exc.status, exc.code, exc.extra


def at_forward(m: AeriMachine):
    """The forward loop of round 1 (r08: 34 journal lines, step 66 of 200)."""
    m.start()
    m.script(journal=34, screen="当前正向子任务 步 66/200", cbreak=True, steps=9)
    return m.wait_key("verdict_success")


def at_release(m: AeriMachine):
    m.start()
    m.script(journal=26, screen="第 1/5 轮：确认场景就绪", cbreak=True, steps=9)
    return m.wait_key("release_round")


def at_reverse_wait(m: AeriMachine):
    """The reverse loop's first SCENE_ASSESS (r08 seq 49): D waits for the VLM."""
    m.start()
    m.script(journal=50, screen="Home verified: error_mm=6.6", cbreak=True, steps=9)
    return m.look()


def interrupt_line(name="SIGINT") -> str:
    """The line D prints when it got a Ctrl+C, rendered from the profile's own
    template (the words of the real code)."""
    texts = sc.prompts()["profiles"]["harvest-5bc750ca"]["texts"]["zh"]
    return texts["interrupt"].format(name=name) + "\r\n"


def operator_interrupt_note(seq=60) -> dict:
    return {
        "sequence_no": seq,
        "record": "note",
        "note": {"code": "operator_interrupt", "detail": "SIGINT"},
    }


def stop_script():
    """``scripts/aeri_page_stop.py`` as a module (its functions are the real ones)."""
    spec = importlib.util.spec_from_file_location("aeri_page_stop_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stop_from_elsewhere(m: AeriMachine, when=None):
    """What ``aeri_page_stop.py`` leaves in the record after its SIGINT."""
    stop_script().mark_sent(m.folder / "record.json", when or time.time())
