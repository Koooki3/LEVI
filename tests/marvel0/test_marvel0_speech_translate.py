"""``levi/marvel0/speech_translate.py``: which machine translations of a Chinese
object name the voice card may use.

The fixture holds the answers Qwen3-4B-Instruct-2507 gave in the 2026-10-31
evaluation (levi-hub programs/rig-console/reviews/voice-translate-20261031.md):
81 names outside the lexicon, 11 words that are not objects, and a held-out
set of 40 frozen before it was run. Replaying them pins the rule to the numbers
the documentation reports; a change of the rule that moves them fails here.
"""

import json
from pathlib import Path

import pytest

from levi.marvel0 import speech_lexicon as lexicon
from levi.marvel0 import speech_translate as translate

ANSWERS = json.loads(
    (Path(__file__).parent / "fixtures" / "speech_translate_answers.json").read_text()
)


def replay(name: str) -> tuple[list, list]:
    """``(passed, passed but not one of the accepted English names)``."""
    passed, off = [], []
    for row in ANSWERS["sets"][name]:
        verdict = translate.judge(
            row["said"], row["english"], row["chinese"], row["same"]
        )
        if verdict.ok:
            passed.append(verdict)
            if verdict.english not in [g.lower() for g in row["gold"]]:
                off.append((row["said"], verdict.english))
    return passed, off


def test_the_recorded_new_names_give_the_documented_numbers():
    passed, off = replay("new")
    assert len(ANSWERS["sets"]["new"]) == 81 and len(passed) == 69
    # Two near misses and three usable variants: what the docs say.
    assert sorted(off) == sorted(
        [
            ("哈密瓜", "honeydew"),
            ("杯垫", "cup pad"),
            ("衣夹", "clothing clip"),
            ("羽毛球", "badminton birdie"),
            ("毛线球", "wool ball"),
        ]
    )


def test_the_recorded_held_out_names_give_the_documented_numbers():
    passed, off = replay("heldout")
    assert len(ANSWERS["sets"]["heldout"]) == 40 and len(passed) == 31
    assert sorted(off) == sorted(
        [
            ("荧光笔", "fluorescent pen"),
            ("笔袋", "pen case"),
            ("茄汁罐头", "tomato cans"),
        ]
    )


def test_no_recorded_word_that_is_not_an_object_passes():
    passed, _ = replay("not_object")
    assert len(ANSWERS["sets"]["not_object"]) == 11 and passed == []


@pytest.mark.parametrize(
    ("said", "english", "back"),
    [
        ("卷笔刀", "eraser", "橡皮擦"),
        ("沐浴露", "shampoo", "洗发水"),
        ("毽子", "jump rope", "跳绳"),
        ("漏勺", "sieve", "筛子"),
    ],
)
def test_a_wrong_translation_is_caught_by_its_back_translation(said, english, back):
    # Even when the model's own judgement says "same".
    verdict = translate.judge(said, english, back, True)
    assert verdict.reason == "unchecked" and not verdict.ok
    assert verdict.english == english and verdict.back == back


@pytest.mark.parametrize(
    ("said", "english", "back", "same", "check"),
    [
        ("金桔", "kumquat", "金桔", None, "back_translation"),
        ("金桔", "kumquat", "金橘", None, "back_translation"),  # 桔 = 橘
        ("柿子", "persimmon", "柿", None, "back_translation"),  # 子 dropped
        ("乐高", "lego", "乐高积木", True, "back_translation_and_judgement"),
        ("膠棒", "glue stick", "胶棒", None, "back_translation"),  # traditional
        ("花椰菜", "cauliflower", "花菜", True, "back_translation_and_judgement"),
    ],
)
def test_a_checked_translation_passes(said, english, back, same, check):
    verdict = translate.judge(said, english, back, same)
    assert verdict.ok and verdict.reason is None
    assert (verdict.english, verdict.check) == (english, check)


def test_sharing_characters_is_not_enough_without_the_models_judgement():
    assert translate.judge("花椰菜", "cauliflower", "花菜", None).reason == "unchecked"
    assert translate.judge("花椰菜", "cauliflower", "花菜", False).reason == "unchecked"
    # One shared character of two is half; one of three is not.
    assert translate.judge("甜椒", "bell pepper", "青椒", True).ok
    assert translate.judge("圆珠笔", "gel pen", "中性笔", True).reason == "unchecked"


@pytest.mark.parametrize(
    "answer",
    [
        None,
        "",
        "NONE",
        "none",
        "left",  # a direction
        "carefully",  # a manner
        "red",  # only a colour
        "big red",  # only modifiers
        "put it",  # an action
        "the cup on the plate",  # more than three words
        "kumquat (a citrus)",  # not plain words
        "金桔",
    ],
)
def test_an_answer_that_is_not_an_object_name_is_refused(answer):
    verdict = translate.judge("金桔", answer, "金桔", True)
    assert verdict.reason == "not_object" and verdict.english is None


def test_the_name_is_cleaned_like_the_templates_other_names():
    verdict = translate.judge("杯垫", "A Coaster.", "杯垫", True)
    assert verdict.ok and verdict.english == "coaster"


@pytest.mark.parametrize(
    ("said", "english"),
    [("金桔", "orange"), ("蜡笔", "pencil"), ("皮球", "ball"), ("皮球", "balls")],
)
def test_an_object_the_lexicon_knows_is_refused_with_its_chinese_names(said, english):
    verdict = translate.judge(said, english, said, True)
    assert verdict.reason == "known_as" and not verdict.ok
    concept = lexicon.EN_CONCEPTS[english.removesuffix("s")]
    assert verdict.known_as and all(
        lexicon.ZH_CONCEPTS[form] == concept for form in verdict.known_as
    )


def test_missing_answers_are_never_accepted():
    assert translate.judge("金桔", "kumquat", None, True).reason == "unchecked"
    assert translate.judge("金桔", "kumquat", "", True).reason == "unchecked"
    assert translate.judge("金桔", "kumquat", "kumquat", True).reason == "unchecked"


@pytest.mark.parametrize(
    ("said", "english", "back"),
    [
        ("牙刷架", "toothbrush", "牙刷"),
        ("鼠标垫", "mouse", "鼠标"),
        ("眼镜盒", "glasses", "眼镜"),
        ("红色榴莲", "durian", "榴莲"),
    ],
)
def test_a_back_translation_that_is_only_part_of_the_name_is_refused(
    said, english, back
):
    # The translation dropped what made it another object, judged same or not.
    assert translate.judge(said, english, back, True).reason == "unchecked"


def test_a_longer_back_translation_needs_the_models_judgement():
    assert translate.judge("乐高", "lego", "乐高积木", None).reason == "unchecked"
    assert translate.judge("牙刷", "toothbrush holder", "牙刷架", False).reason == (
        "unchecked"
    )


@pytest.mark.parametrize(
    "english", ["peaches", "potatoes", "tomatoes", "strawberries", "boxes"]
)
def test_a_plural_of_a_lexicon_object_is_known(english):
    assert translate.judge("某物", english, "某物", True).reason == "known_as"


@pytest.mark.parametrize(
    "english",
    [
        "hold on",  # a stop phrase
        "hold it",
        "followed by",  # a step separator
        "apple and banana",
        "cup with lid",
        "beside bowl",
        "on plate",
        "pick up",
    ],
)
def test_a_phrase_that_is_not_one_object_is_refused(english):
    assert translate.judge("某物", english, "某物", True).reason == "not_object"


@pytest.mark.parametrize("back", [3, ["金桔"], b"x"])
def test_an_answer_of_the_wrong_type_never_raises(back):
    assert translate.judge("金桔", "kumquat", back, True).reason == "unchecked"
