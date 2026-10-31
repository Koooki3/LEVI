"""The speech routes translate Chinese object names outside the lexicon with
the loaded worker's text model, and use only what ``speech_translate.judge``
passes. A fake manager answers; the real matcher, template and catalog reader
run."""

from collections import OrderedDict

import pytest
from test_marvel0_speech_generated_route import catalog_of  # noqa: F401 - fixture
from test_marvel0_speech_route import (
    BASE,
    REQ,
    TRANSCRIBE,
    FakeManager,
    build_machine,
    policy_status,
    transcript,
)

from levi.marvel0 import models, speech_api
from levi.marvel0 import site as marvel0_site
from levi.stt import settings
from levi.stt.errors import SttError
from levi.stt.manager import Translation

SAID = "把金桔放到蓝色碗里"
ANSWERS = {
    "金桔": {"english": "kumquat", "chinese": "金桔", "same": True},
    "卷笔刀": {"english": "eraser", "chinese": "橡皮擦", "same": True},
    "皮球": {"english": "ball", "chinese": "球", "same": True},
}


class TranslatingManager(FakeManager):
    def __init__(self):
        super().__init__()
        self.view["state"] = "ready"
        self.translated = []
        self.translate_error = None
        self.text_loaded = False

    def can_translate(self):
        return None if self.view["state"] == "ready" else "not_loaded"

    def translate(self, request_id, names, may_load=None, timeout_s=20):
        self.translated.append((request_id, list(names), timeout_s))
        if self.translate_error:
            raise self.translate_error
        if not self.text_loaded and may_load is not None:
            may_load()
        self.text_loaded = True
        items = tuple(
            {
                "name": n,
                **ANSWERS.get(n, {"english": "NONE", "chinese": None, "same": None}),
            }
            for n in names
        )
        return Translation(items, loaded_now=True, load_ms=1500, infer_ms=850)


@pytest.fixture
def machine(tmp_path, monkeypatch, catalog_of):  # noqa: F811
    monkeypatch.setattr(speech_api, "_translated", OrderedDict())
    monkeypatch.setattr(speech_api, "TRANSLATE_RATE", speech_api.RateLimit(100, 60.0))
    monkeypatch.setattr(settings, "translation_state", lambda: "ready")
    catalog_of(
        "a", ["pick peach on green plate"] * 5 + ["put eggplant into blue bowl"] * 2
    )
    yield build_machine(tmp_path, monkeypatch, TranslatingManager(), None)
    marvel0_site.clear_cache()
    models.cache_clear()


def match(machine, said=SAID, **body):
    response = machine.client.post(f"{BASE}/match", json={"transcript": said, **body})
    assert response.status_code == 200
    return response.json()["match"]["generated"]


def test_a_checked_translation_fills_the_slot(machine):
    generated = match(machine)
    assert generated["status"] == "ok"
    assert generated["prompt"] == "pick kumquat into blue bowl"
    assert generated["slots"][0] == {
        "said": "金桔",
        "english": "kumquat",
        "source": "translation",
    }
    assert generated["translations"] == [
        {
            "said": "金桔",
            "english": "kumquat",
            "back": "金桔",
            "check": "back_translation",
            "ok": True,
            "reason": None,
            "known_as": [],
        }
    ]
    assert generated["untranslated_why"] is None
    [(request_id, names, timeout_s)] = machine.manager.translated
    assert request_id.startswith("match:") and names == ["金桔"]
    assert 0 < timeout_s <= settings.MATCH_BUDGET_S
    assert machine.manager.calls == [], "the match route never transcribes"


def test_a_name_is_asked_once_and_then_answered_from_memory(machine):
    match(machine)
    again = match(machine)
    assert again["prompt"] == "pick kumquat into blue bowl"
    assert len(machine.manager.translated) == 1


def test_a_wrong_translation_is_shown_and_not_used(machine):
    generated = match(machine, "把卷笔刀放到蓝色碗里")
    assert (generated["status"], generated["prompt"]) == ("untranslated", None)
    assert generated["untranslated"] == ["卷笔刀"]
    [verdict] = generated["translations"]
    assert verdict["ok"] is False and verdict["reason"] == "unchecked"
    assert (verdict["english"], verdict["back"]) == ("eraser", "橡皮擦")
    assert generated["untranslated_why"] is None


def test_an_object_the_lexicon_knows_is_named_with_its_chinese_words(machine):
    generated = match(machine, "把皮球放到蓝色碗里")
    [verdict] = generated["translations"]
    assert verdict["reason"] == "known_as" and "球" in verdict["known_as"]
    assert generated["status"] == "untranslated"


def test_an_english_or_lexicon_sentence_never_asks_the_model(machine):
    assert match(machine, "把茄子放到蓝色碗里")["status"] == "ok"
    assert match(machine, "put the zucchini into the blue bowl")["status"] == "ok"
    assert machine.manager.translated == []


def test_a_restricted_model_never_asks_the_model(machine):
    generated = match(machine, model_id="pi05_fr3_sp_v1")
    assert generated["status"] == "restricted" and machine.manager.translated == []


@pytest.mark.parametrize(
    ("code", "why"),
    [
        ("not_loaded", "not_loaded"),
        ("busy", "busy"),
        ("translate_unavailable", "not_installed"),
        ("translate_gpu", "gpu_memory"),
        ("timeout", "failed"),
        ("failed", "failed"),
        ("no_time", "no_time"),
    ],
)
def test_why_a_name_got_no_translation_is_said(machine, code, why):
    machine.manager.translate_error = SttError(code, "x")
    generated = match(machine)
    assert generated["status"] == "untranslated" and generated["translations"] == []
    assert generated["untranslated_why"] == why
    # A failure is not remembered: the next request asks again.
    machine.manager.translate_error = None
    assert match(machine)["status"] == "ok"


@pytest.mark.parametrize(
    ("state", "why"), [("off", "off"), ("model_missing", "not_installed")]
)
def test_translation_off_or_missing_asks_nobody(machine, monkeypatch, state, why):
    monkeypatch.setattr(settings, "translation_state", lambda: state)
    generated = match(machine)
    assert generated["untranslated_why"] == why and machine.manager.translated == []


def test_a_closed_gate_asks_nobody(machine):
    machine.policy.doc = policy_status(established=1)
    generated = match(machine)
    assert generated["untranslated_why"] == "blocked"
    assert machine.manager.translated == []


def test_the_text_model_loads_only_with_room_on_the_gpu(machine):
    needed = settings.TRANSLATE_VRAM_MIB + 1024  # the sample site's margin
    machine.policy.doc = policy_status(free_mib=needed - 1)
    assert match(machine)["untranslated_why"] == "gpu_memory"
    machine.policy.doc = policy_status(free_mib=needed)
    assert match(machine)["status"] == "ok"


def test_the_memory_is_read_fresh_before_the_load_and_only_then(machine):
    # The cached status may be from before the speech worker itself loaded.
    match(machine)
    assert machine.policy.invalidated == 1
    match(machine, "把荔枝放到蓝色碗里")  # the text model is loaded now
    assert machine.policy.invalidated == 1


def test_translations_have_their_own_rate_limit(machine, monkeypatch):
    monkeypatch.setattr(speech_api, "TRANSLATE_RATE", speech_api.RateLimit(1, 60.0))
    assert match(machine)["status"] == "ok"
    assert match(machine, "把荔枝放到蓝色碗里")["untranslated_why"] == "rate_limit"
    assert len(machine.manager.translated) == 1


def test_a_refusal_before_the_worker_uses_no_rate_limit_slot(machine, monkeypatch):
    limiter = speech_api.RateLimit(1, 60.0)
    monkeypatch.setattr(speech_api, "TRANSLATE_RATE", limiter)
    machine.manager.view["state"] = "off"
    for _ in range(3):
        assert match(machine)["untranslated_why"] == "not_loaded"
    machine.manager.view["state"] = "ready"
    assert match(machine)["status"] == "ok"
    assert machine.manager.translated and limiter.take() > 0


def test_with_no_time_left_nothing_is_asked(machine, monkeypatch):
    monkeypatch.setattr(settings, "MATCH_BUDGET_S", 1)
    assert match(machine)["untranslated_why"] == "no_time"
    assert machine.manager.translated == []


def test_more_names_than_the_worker_takes_are_not_asked(machine):
    said = "把金桔放到荔枝上然后把芒果放到菠萝上然后把榴莲放到碗里"
    generated = match(machine, said)
    assert generated["status"] == "untranslated"
    assert generated["untranslated_why"] == "too_many"
    assert machine.manager.translated == []


@pytest.mark.parametrize(
    "said",
    ["不要把金桔放到蓝色碗里", "把金桔从蓝色碗里拿出来", "金桔"],
)
def test_a_sentence_that_fails_for_another_reason_never_asks_the_model(machine, said):
    assert match(machine, said)["status"] != "untranslated"
    assert machine.manager.translated == []


def test_a_transcription_translates_within_the_pages_wait(machine):
    machine.manager.answer = transcript(SAID)
    response = machine.client.post(
        TRANSCRIBE,
        content=__import__("test_marvel0_speech_route").wav_bytes(),
        headers={"content-type": "audio/wav"},
    )
    assert response.status_code == 200
    generated = response.json()["match"]["generated"]
    assert generated["prompt"] == "pick kumquat into blue bowl"
    [(request_id, names, timeout_s)] = machine.manager.translated
    assert request_id == f"{REQ}:names" and names == ["金桔"]
    assert settings.MATCH_BUDGET_S < timeout_s <= settings.TRANSCRIBE_BUDGET_S
