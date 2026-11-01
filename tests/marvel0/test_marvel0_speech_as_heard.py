"""``match.as_heard`` of the speech answers: the transcript made tidy as a task
instruction ("pick tennis from white plate to green plate." -> lower case, no
trailing punctuation) and the reason the client command card would refuse it,
taken from the card's own check (``command.prompt_problem``), so the page can
offer "the sentence as heard" beside the matched and generated instructions."""

import logging

import pytest
from test_marvel0_speech_route import (
    BASE,
    REQ,
    FakeManager,
    Matcher,
    build_machine,
    transcript,
)

from levi.marvel0 import command, models, speech_api
from levi.marvel0 import site as marvel0_site

SAID = "Pick tennis from white plate to green plate."
TIDY = "pick tennis from white plate to green plate"
RESTRICTED = "pi05_fr3_sp_v1"
ALL = "pi05_fr3_all_step49999"


@pytest.fixture
def machine(tmp_path, monkeypatch):
    yield build_machine(tmp_path, monkeypatch, FakeManager(), Matcher())
    marvel0_site.clear_cache()
    models.cache_clear()


def text_match(machine, text, model_id=None):
    body = {"transcript": text}
    if model_id:
        body["model_id"] = model_id
    response = machine.client.post(f"{BASE}/match", json=body)
    assert response.status_code == 200, response.text
    return response.json()["match"]["as_heard"]


@pytest.mark.parametrize(
    "said, tidy",
    [
        (SAID, TIDY),
        ("  Pick   the Cup!  ", "pick the cup"),
        ("pick it up?!", "pick it up"),
        ("Pick it. Then stack it.", "pick it. then stack it"),
        ("pick\tthe\ncup .", "pick the cup"),
        ("把网球放进绿色盘子。", "把网球放进绿色盘子"),
        ("把网球放进绿色盘子！", "把网球放进绿色盘子"),
        ("。", ""),
        ("...", ""),
        ("   ", ""),
    ],
)
def test_the_sentence_is_trimmed_folded_unpunctuated_and_lower_case(said, tidy):
    assert speech_api.as_heard_sentence(said) == tidy


def test_a_transcription_carries_the_sentence_as_heard(machine):
    machine.manager.answer = transcript(SAID)
    response = machine.post()
    assert response.status_code == 200
    assert response.json()["match"]["as_heard"] == {"prompt": TIDY, "problem": None}
    # The matcher was given the transcript itself, not the tidy sentence.
    assert machine.matcher.calls[0][0] == SAID


def test_an_edited_text_carries_it_too(machine):
    assert text_match(machine, "Stack the Plates.") == {
        "prompt": "stack the plates",
        "problem": None,
    }


def test_a_sentence_with_nothing_left_has_no_such_option(machine):
    assert text_match(machine, "...") is None
    machine.manager.answer = transcript("?!")
    assert machine.post().json()["match"]["as_heard"] is None


@pytest.mark.parametrize(
    "said, problem",
    [
        ("pick the cup's lid", "forbidden_char"),
        ('pick the "red" cup', "forbidden_char"),
        ("pick the cup $HOME", "forbidden_char"),
        ("pick the cup\\", "forbidden_char"),
        ("pick `ls` cup", "forbidden_char"),
        ("wow! pick the cup", "forbidden_char"),
        ("-pick the cup", "leading_dash"),
        ("pick the cup\u200b now", "control_char"),
        ("a " * 160, "too_long"),
    ],
)
def test_a_sentence_the_card_refuses_says_why(machine, said, problem):
    assert text_match(machine, said, ALL)["problem"] == problem
    # The same answer with no model: the card's text rule alone.
    assert text_match(machine, said)["problem"] == problem


def test_the_reason_is_the_command_cards_own_check_not_a_copy(machine, monkeypatch):
    asked = []

    def refuse(text, site, model):
        asked.append((text, model.id))
        return "forbidden_char"

    monkeypatch.setattr(command, "prompt_problem", refuse)
    assert text_match(machine, SAID, ALL) == {
        "prompt": TIDY,
        "problem": "forbidden_char",
    }
    assert asked == [(TIDY, ALL)]


def test_a_restricted_model_takes_only_its_trained_instructions(machine):
    assert text_match(machine, SAID, RESTRICTED) == {
        "prompt": TIDY,
        "problem": "unknown_for_model",
    }
    machine.manager.answer = transcript(SAID)
    response = machine.post(query=f"?request_id={REQ}&model_id={RESTRICTED}")
    assert response.json()["match"]["as_heard"]["problem"] == "unknown_for_model"
    assert response.json()["match"]["family"] == "restricted"


def test_chinese_stays_chinese(machine):
    assert text_match(machine, "把网球放进绿色盘子。", ALL) == {
        "prompt": "把网球放进绿色盘子",
        "problem": None,
    }


def test_a_check_that_breaks_never_fails_the_match_and_logs_no_text(
    machine, monkeypatch, caplog
):
    def broken(text, site, model):
        raise RuntimeError(f"cannot check {text}")

    monkeypatch.setattr(command, "prompt_problem", broken)
    caplog.set_level(logging.DEBUG)
    response = machine.client.post(
        f"{BASE}/match", json={"transcript": SAID, "model_id": ALL}
    )
    assert response.status_code == 200
    assert response.json()["match"]["as_heard"] is None
    assert "tennis" not in caplog.text and TIDY not in caplog.text
    assert "the sentence as heard was not checked (RuntimeError)" in caplog.text


def test_the_sentence_as_heard_is_never_written_to_a_log(machine, caplog):
    caplog.set_level(logging.DEBUG)
    machine.manager.answer = transcript(SAID)
    assert machine.post().status_code == 200
    assert text_match(machine, "Pick the Sentinel Cup.") is not None
    assert "tennis" not in caplog.text.lower()
    assert "sentinel" not in caplog.text.lower()
