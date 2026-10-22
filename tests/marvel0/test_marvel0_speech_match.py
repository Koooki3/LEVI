"""``levi/marvel0/speech_match.py``: the closed-list matcher behind voice input.

The property that matters is one-sided: a spoken sentence may be turned away
(tier B, C or V) as often as the matcher likes, but it must never be *accepted*
(tier A, one instruction pre-selected) for an instruction the person did not
mean. So most tests count wrong accepts over hand-written and generated
sentences in three worlds (the allowed lists of the restricted model family,
the site file and the 53 training tasks), and every such count must be zero.
The correct-accept counts are lower bounds so that tuning cannot silently
throw the matcher away.

The corpora are data (``fixtures/speech_*.json``); the generated near misses are
code (``mutations``). Invisible characters are built with ``chr()`` here for
the reason in the matcher: an escape sequence in a source file can be turned
into the real character by tools.
"""

import collections
import json
import random
import re
import statistics
import subprocess
import sys
import time
from functools import cache
from pathlib import Path

import pytest

from levi.marvel0 import site as marvel0_site
from levi.marvel0 import speech_lexicon as lexicon
from levi.marvel0 import speech_match as M

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[2]
WORLD_NAMES = ("W2", "W5", "W53")
BASE = "pick the eggplant in the blue plate"


def chars(*codes):
    return "".join(chr(code) for code in codes)


@cache
def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@cache
def worlds():
    prompts = fixture("speech_prompts.json")
    return {
        "W2": tuple(prompts["restricted"]),
        "W5": tuple(prompts["site_known"]),
        "W53": tuple(dict.fromkeys(prompts["site_known"] + prompts["training_tasks"])),
    }


@cache
def hand_corpus():
    rows = []
    for name in ("speech_dev.json", "speech_held1.json", "speech_held2.json"):
        rows += fixture(name)
    return tuple(rows)


def match(text, world="W53", family="all"):
    return M.match_transcript(text, list(worlds()[world]), family=family)


def chosen(result):
    return result["best"]["prompt"] if result["best"] else None


def shown(result):
    """The instructions the person is offered, best first."""
    if not result["best"]:
        return []
    return [result["best"]["prompt"]] + [a["prompt"] for a in result["alternatives"]]


# ------------------------------------------------- generated near misses ----
COLORS = ["blue", "red", "white", "green", "brown", "pink"]
FOOD = ["eggplant", "tomato", "carrot", "apple", "orange", "peach", "cucumber"]
FOOD += ["mushroom", "potato", "pepper", "croissant"]
VESSEL = ["plate", "cup", "bowl", "basket"]
TOOL = ["fork", "spoon", "knife"]
VERB = ["pick", "stack", "pour"]
MOVE_VERBS = {"pick", "put", "place"}
PREPOSITIONS = ["on", "in", "into"]


def mutations(prompt):
    """Near misses of ``prompt``: one thing changed, or a command added."""
    out = {}
    words = prompt.split()
    for i, word in enumerate(words):
        groups = [COLORS, FOOD, VESSEL, TOOL, VERB]
        if word in MOVE_VERBS:
            groups = [["stack", "pour", word]]
        for group in groups:
            if word in group:
                for other in group:
                    if other != word:
                        changed = " ".join(words[:i] + [other] + words[i + 1 :])
                        out[changed] = f"swap {word}->{other}"
    if "same" in words:
        out[prompt.replace("same", "different")] = "same->different"
    for color in COLORS:
        if color in words:
            out[" ".join(w for w in words if w != color)] = f"drop {color}"
    if " then " in prompt:
        first, second = prompt.split(" then ", 1)
        out[second + " then " + first] = "reverse clauses"
        out[first] = "drop 2nd clause"
        out[second] = "drop 1st clause"
        for k, clause in enumerate((first, second)):
            found = re.match(
                r"(stack|place|put|pick) (\w+ \w+|\w+) on (\w+ \w+|\w+)$", clause
            )
            if found:
                swapped = f"{found.group(1)} {found.group(3)} on {found.group(2)}"
                both = (swapped, second) if k == 0 else (first, swapped)
                out[" then ".join(both)] = "swap entities in clause"
    found = re.match(r"(stack|put|pick|place) ([\w ]+?) (on|into) ([\w ]+)$", prompt)
    if found:
        out[f"{found.group(1)} {found.group(4)} {found.group(3)} {found.group(2)}"] = (
            "swap entities"
        )
    for prefix in ("don't ", "do not ", "stop ", "never ", "cancel "):
        out[prefix + prompt] = "negation/stop prefix"
    out[prompt + " and then open the drawer"] = "extra command"
    out[prompt + " twice"] = "extra word"
    return out


@cache
def mutation_corpus():
    """Every near miss of every one of the 53 instructions, labelled with the
    instruction it is really another way to say (``None`` if none). Against a
    smaller world the near misses of the other instructions are extra negatives."""
    universe = worlds()["W53"]
    by_meaning = {}
    for prompt in universe:
        by_meaning.setdefault(M._signature(prompt), prompt)
    rows, seen = [], set(universe)
    for prompt in universe:
        for text, why in mutations(prompt).items():
            if text in seen:
                continue
            seen.add(text)
            kind = why.split()[0]
            gold = None
            if kind not in ("negation/stop", "extra"):
                gold = by_meaning.get(M._signature(text))
            rows.append({"text": text, "cat": "mut:" + kind, "gold": gold})
        words = prompt.split()
        for i, word in enumerate(words):
            if i == 0 and word in MOVE_VERBS:
                others, cat = sorted(MOVE_VERBS - {word}), "syn:verb"
            elif word in PREPOSITIONS:
                others, cat = [p for p in PREPOSITIONS if p != word], "syn:prep"
            else:
                continue
            for other in others:
                text = " ".join(words[:i] + [other] + words[i + 1 :])
                if text not in seen:
                    seen.add(text)
                    rows.append({"text": text, "cat": cat, "gold": prompt})
    return tuple(rows)


def noisy(text, rate, rnd):
    """``text`` with random letter slips (substitute, delete, double, swap)."""
    neighbours = {
        "a": "sqz",
        "e": "wrd",
        "i": "uok",
        "o": "ipl",
        "u": "yij",
        "s": "adw",
    }
    neighbours |= {
        "t": "ryg",
        "r": "etf",
        "c": "xvd",
        "l": "kop",
        "n": "bmh",
        "m": "nj",
    }
    letters = "abcdefghijklmnopqrstuvwxyz"
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch.isalpha() and rnd.random() < rate:
            op = rnd.choice(["sub", "del", "dup", "swap"])
            if op == "sub":
                out.append(rnd.choice(neighbours.get(ch, letters)))
            elif op == "dup":
                out.append(ch + ch)
            elif op == "swap" and i + 1 < len(text):
                out.append(text[i + 1] + ch)
                i += 1
            elif op != "del":
                out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


@cache
def noise_corpus():
    rnd = random.Random(7)
    rows = []
    for rate in (0.02, 0.05, 0.10):
        for prompt in worlds()["W5"]:
            rows += [
                {
                    "text": noisy(prompt, rate, rnd),
                    "cat": f"noise{rate}",
                    "gold": prompt,
                }
                for _ in range(40)
            ]
    return tuple(rows)


# ------------------------------------------------------------- scoring runs ----
def gold_in(row, world):
    return row["gold"] if row["gold"] in worlds()[world] else None


@cache
def run(corpus_name, world):
    """(row, gold, result) for every row of a corpus in a world."""
    corpus = {
        "hand": hand_corpus,
        "mutations": mutation_corpus,
        "noise": noise_corpus,
    }[corpus_name]()
    allowed = list(worlds()[world])
    return tuple(
        (
            row,
            gold_in(row, world),
            M.match_transcript(row["text"], allowed, family="all"),
        )
        for row in corpus
    )


def wrong_accepts(corpus_name, world):
    return [
        (row["text"], chosen(result), gold)
        for row, gold, result in run(corpus_name, world)
        if result["tier"] == "A" and chosen(result) != gold
    ]


def correct_accepts(corpus_name, world):
    return sum(
        1
        for _, gold, result in run(corpus_name, world)
        if result["tier"] == "A" and chosen(result) == gold is not None
    )


def positives(corpus_name, world):
    return sum(1 for _, gold, _ in run(corpus_name, world) if gold is not None)


# --------------------------------------------------------------- fixtures ----
def test_fixture_worlds_are_the_documented_sets():
    prompts = fixture("speech_prompts.json")
    assert len(prompts["site_known"]) == 5
    assert prompts["restricted"] == list(marvel0_site.RESTRICTED_PROMPTS)
    assert prompts["restricted"] == prompts["site_known"][:2]
    assert len(prompts["training_tasks"]) == len(set(prompts["training_tasks"])) == 50
    assert [len(worlds()[w]) for w in WORLD_NAMES] == [2, 5, 53]


def test_fixture_corpora_have_the_documented_size():
    assert [len(fixture(f"speech_{n}.json")) for n in ("dev", "held1", "held2")] == [
        163,
        110,
        102,
    ]
    assert [positives("hand", w) for w in WORLD_NAMES] == [47, 129, 168]
    assert len({row["text"] for row in hand_corpus()}) >= 370  # a few repeats at most


# ------------------------------------------------ the safety property itself ----
@pytest.mark.parametrize("world", WORLD_NAMES)
def test_no_wrong_accept_on_the_hand_written_corpora(world):
    """A sentence accepted at tier A for another instruction, or any accept of a
    sentence that means none (stop, negation, out of set, injection), is wrong."""
    assert wrong_accepts("hand", world) == []


@pytest.mark.parametrize("world", WORLD_NAMES)
def test_no_wrong_accept_on_generated_near_misses(world):
    assert wrong_accepts("mutations", world) == []


def test_generated_near_misses_have_the_documented_size():
    assert len(mutation_corpus()) == 1611
    assert [positives("mutations", w) for w in WORLD_NAMES] == [4, 22, 218]


@pytest.mark.parametrize("world", ["W5", "W53"])
def test_synonym_near_misses_are_accepted(world):
    """pick/put/place and in/into/on are one task: every such variant is accepted."""
    synonyms = [
        r for r in run("mutations", world) if r[0]["cat"].startswith("syn:") and r[1]
    ]
    assert len(synonyms) >= 10
    assert all(res["tier"] == "A" and chosen(res) == gold for _, gold, res in synonyms)


@pytest.mark.parametrize("world", ["W5", "W53"])
def test_no_wrong_accept_under_letter_noise(world):
    assert wrong_accepts("noise", world) == []


@pytest.mark.parametrize("world", WORLD_NAMES)
def test_highest_wrong_score_keeps_its_margin_below_the_accept_line(world):
    """The best-scoring *wrong* top candidate (vetoed sentences excluded) was 88
    when this was measured: the accept line (90) has two points of margin. A
    penalty edit that eats the margin shows up here before it shows up as a
    wrong accept."""
    worst = 0
    for corpus in ("hand", "mutations", "noise"):
        for _, gold, result in run(corpus, world):
            if result["tier"] in ("A", "B") and chosen(result) != gold:
                worst = max(worst, result["best"]["score"])
    assert worst <= 88


@pytest.mark.parametrize(("world", "bound"), [("W2", 30), ("W5", 96), ("W53", 135)])
def test_correct_accepts_are_at_least_the_measured_lower_bound(world, bound):
    assert correct_accepts("hand", world) >= bound


@pytest.mark.parametrize("world", WORLD_NAMES)
def test_a_positive_that_is_not_accepted_is_offered_with_the_right_one_first(world):
    """Tier B must lead with the instruction the person meant (it is never
    pre-selected, so the order is the whole help)."""
    first = other = 0
    for _, gold, result in run("hand", world):
        if gold is None or result["tier"] != "B":
            continue
        assert gold in shown(result)
        if shown(result)[0] == gold:
            first += 1
        else:
            other += 1
    assert first >= 10 and other == 0


def test_the_tier_of_every_outcome_agrees_with_its_score():
    """No list here holds two instructions that fit one sentence, so tier A is
    exactly "score of 90 or more" and tier B exactly "60 to 89"."""
    tiers = collections.Counter()
    for corpus in ("hand", "mutations", "noise"):
        for world in WORLD_NAMES:
            for _, _, res in run(corpus, world):
                tiers[res["tier"]] += 1
                if res["tier"] == "A":
                    assert res["best"]["score"] >= 90 and res["flag"] is None
                elif res["tier"] == "B":
                    assert 60 <= res["best"]["score"] < 90 and res["flag"] is None
                elif res["tier"] == "C":
                    assert res["best"] is None and res["alternatives"] == []
                    assert res["flag"] is None
                else:
                    assert res["tier"] == "V" and res["flag"]
                    assert res["best"] is None and res["alternatives"] == []
    assert set(tiers) == {"A", "B", "C", "V"}


def test_dropping_one_word_of_an_instruction_never_selects_another_one():
    """Every single-word dropout of the 53 instructions: tier A only for the
    same instruction, and only when the word is one that carries no task."""
    redundant = {"on", "the", "into", "of", "and", "in", "onto", "it", "up", "together"}
    redundant |= {"pick", "put", "toy", "test"}  # a second verb / a synonym half
    allowed = list(worlds()["W53"])
    for prompt in allowed:
        words = prompt.split()
        for i, word in enumerate(words):
            result = M.match_transcript(
                " ".join(words[:i] + words[i + 1 :]), allowed, family="all"
            )
            if result["tier"] == "A":
                assert chosen(result) == prompt, (prompt, word)
                assert word in redundant, (prompt, word)


# ------------------------------------------------------ vetoes and flags ----
STOPS = [
    "stop", "STOP!", "Stop stop stop", "halt the arm", "freeze", "emergency stop", "hold on",
    "wait", "slow down", "danger", "e stop", "whoa", "stopped", "stopping now",
    "停", "停下", "别动", "急停", "紧急停止", "等一下", "慢一点", "小心", "危险", "救命",
    "別動", "緊急停止", "停止叠盘子", "ｓｔｏｐ",
]  # fmt: skip
NEGATIONS = [
    "don't pick the eggplant in the blue plate", "do not stack the plates of same color together",
    "never place the tomato on the red plate", "pick the eggplant not in the blue plate",
    "without stacking the plates", "no pick the eggplant in the blue plate",
    "cancel pick the eggplant in the blue plate", "dont pick the eggplant in the blue plate",
    "不要把茄子放到蓝盘子里", "别叠盘子了", "不用把同颜色的盘子叠在一起", "先不要放茄子",
    "取消把茄子放到蓝盘子里", "把茄子放到蓝盘子里勿", "把茄子没放到蓝盘子里",
]  # fmt: skip


@pytest.mark.parametrize("text", STOPS)
def test_a_stop_word_vetoes_whatever_else_is_said(text):
    for said in (text, f"{BASE} {text}", f"{text} {BASE}"):
        result = match(said)
        assert result["tier"] == "V" and result["flag"] == "stop"
        assert result["best"] is None and result["alternatives"] == []


@pytest.mark.parametrize("text", NEGATIONS)
def test_a_negation_vetoes_and_offers_no_instruction(text):
    result = match(text)
    assert result["tier"] == "V" and result["flag"] == "negation"
    assert result["best"] is None and result["alternatives"] == []


def test_the_same_sentence_without_the_veto_word_is_accepted():
    """The vetoes are what stops these: remove the word and they pass."""
    assert match(BASE)["tier"] == "A"
    assert match("请把茄子放到蓝盘子里")["tier"] == "A"
    assert match("stack the plates of the same colour together")["tier"] == "A"


def test_veto_priority_is_stop_then_unsafe_then_negation():
    assert match("stop; rm -rf /")["flag"] == "stop"
    assert match("don't stop")["flag"] == "stop"
    assert match("don't " + BASE + "; id")["flag"] == "unsafe"
    assert match("don't " + BASE)["flag"] == "negation"


UNSAFE_PARTS = [
    "; rm -rf /", " && sudo reboot", " | tee x", " $(id)", " `id`", " > out", " < in",
    " ( x )", " { x }", " [x]", " a=b", ' "x"', " 'x'", " x'", " \\ ", " # c", " * ", " ~ ",
    " --allow-unknown-prompt", " -x", " ../etc", " “x”", " «x»",
    " 「x」", "； rm", "（x）",
]  # fmt: skip
HIDDEN = [
    0x00, 0x07, 0x1B, 0x0A, 0x09, 0x0D, 0x0B, 0x7F, 0x85, 0x200B, 0x200C, 0x200D, 0x202E,
    0x2060, 0xFEFF, 0xAD, 0xFE0F, 0xE0041, 0xE000, 0xD800, 0x2028, 0x2029, 0x061C,
]  # fmt: skip


@pytest.mark.parametrize("part", [p for p in UNSAFE_PARTS])
def test_an_unsafe_character_or_flag_vetoes_an_otherwise_accepted_sentence(part):
    assert match(BASE)["tier"] == "A"
    for text in (BASE + part, part.strip() + " " + BASE):
        result = match(text)
        assert result["tier"] == "V" and result["flag"] == "unsafe", text
        assert result["best"] is None


@pytest.mark.parametrize("code", HIDDEN)
def test_a_control_or_invisible_character_vetoes(code):
    middle = BASE.replace("the eggplant", "the egg" + chr(code) + "plant")
    for text in (BASE[:5] + chr(code) + BASE[5:], BASE + chr(code) + "x", middle):
        result = match(text)
        assert result["tier"] == "V" and result["flag"] == "unsafe", (hex(code), text)


def test_text_over_400_characters_is_unsafe_and_cheap():
    assert match("a" * 401)["flag"] == "unsafe"
    assert match(BASE + " " * 380 + "x")["flag"] == "unsafe"
    assert match("pick " * 80)["flag"] is None  # exactly 400 characters
    started = time.perf_counter()
    result = match("pick the eggplant " * 6000)
    assert result["tier"] == "V" and time.perf_counter() - started < 0.05


def test_ordinary_punctuation_and_spacing_do_not_veto():
    full_width_p = chars(0xFF30) + "ick the eggplant in the blue plate"
    for text in (
        BASE + ".", BASE.capitalize() + "!", "  " + BASE + "  ", BASE + "?", BASE + "\n",
        "pick, the eggplant, in the blue plate", "pick the eggplant" + chars(0xA0) + "in the blue plate",
        "pick the eggplant in the blue plate" + chars(0x3002), full_width_p,
    ):  # fmt: skip
        assert match(text)["tier"] == "A", text
    assert match("stack the white plate on the upside-down cup")["tier"] != "V"


def test_text_from_a_script_the_lexicon_does_not_cover_is_never_accepted():
    for tail in ("привет", "こんにちは", "한글", "café", "42", "%", "\U0001f916"):
        result = match(f"{BASE} {tail}")
        assert result["tier"] != "A" or tail in ("%", "\U0001f916"), tail


# ------------------------------------------------------------ the corpora ----
def test_the_injection_corpus_never_reaches_tier_a_and_unsafe_text_is_vetoed():
    rows = [r for r in hand_corpus() if r["cat"] == "inject"]
    assert len(rows) == 29
    for world in WORLD_NAMES:
        allowed = list(worlds()[world])
        for row in rows:
            result = M.match_transcript(row["text"], allowed, family="all")
            assert result["tier"] != "A", row["text"]
    flagged = [
        r for r in rows
        if match(r["text"])["flag"] == "unsafe"
    ]  # fmt: skip
    assert len(flagged) >= 26


def test_every_stop_and_negation_row_of_the_corpora_is_vetoed_without_a_candidate():
    for row in hand_corpus():
        if row["cat"] in ("stop", "negation"):
            result = match(row["text"])
            assert result["tier"] == "V", row["text"]
            assert result["best"] is None and result["alternatives"] == []
            assert result["flag"] == ("stop" if row["cat"] == "stop" else "negation")


def test_empty_and_hallucinated_text_matches_nothing():
    for row in hand_corpus():
        if row["cat"] in ("empty", "halluc"):
            assert match(row["text"])["tier"] == "C", row["text"]


@pytest.mark.parametrize(
    "row", fixture("speech_parity.json"), ids=lambda r: r["prompt"][:30]
)
def test_english_and_chinese_sentences_reach_the_same_instruction(row):
    allowed = list(worlds()["W53"])
    english = M.match_transcript(row["en"], allowed, family="all")
    chinese = M.match_transcript(row["zh"], allowed, family="all")
    assert english["tier"] == "A" and chosen(english) == row["prompt"], row["en"]
    assert chinese["tier"] == row.get("zh_tier", "A"), row["zh"]
    assert chosen(chinese) == row["prompt"], row["zh"]


def test_traditional_characters_read_like_simplified_ones():
    assert chosen(match("把茄子放到藍盤子裡")) == BASE
    assert match("把茄子放到藍盤子裡")["tier"] == "A"
    assert match("緊急停止")["flag"] == "stop"


@pytest.mark.parametrize(
    "row", fixture("speech_stt_errors.json"), ids=lambda r: r["text"][:30]
)
def test_recognition_mistakes_are_offered_with_the_right_instruction_first(row):
    result = match(row["text"])
    assert result["tier"] == "B", result
    assert shown(result)[0] == row["prompt"]
    assert result["best"]["fixes"] == [{"heard": row["fix"][0], "as": row["fix"][1]}]
    assert M.TIER_B_MIN <= result["best"]["score"] < M.TIER_A_MIN


def test_a_guessed_word_never_reaches_tier_a():
    """The guard behind the test above: the penalty for a repair is what keeps
    every guess out of the pre-selected tier."""
    assert M.PENALTY["repair"] >= 11
    for row in fixture("speech_stt_errors.json"):
        for world in ("W5", "W53"):
            assert match(row["text"], world)["tier"] != "A"


def test_the_tier_lines_are_the_documented_ones():
    assert (M.TIER_A_MIN, M.TIER_B_MIN) == (90, 60)
    # About something else: the best fit scores 55 and 30, under the offer line.
    assert match("pick the pear in the blue plate", "W5")["tier"] == "C"
    assert match("pick up the banana", "W5")["tier"] == "C"


def test_a_repair_spelling_never_beats_a_plain_word_that_is_as_long(monkeypatch):
    monkeypatch.setitem(M._ZH_ALIASES, "盘", ("N:CUP", "杯"))
    assert match("把茄子放到蓝盘子里", "W5")["tier"] == "A"
    longer = match("把茄子放到蓝盘字里", "W5")
    assert longer["best"]["fixes"] == [{"heard": "盘字", "as": "盘子"}]


def test_ambiguous_sentences_are_never_pre_selected():
    for row in hand_corpus():
        if row["cat"] == "ambig":
            assert match(row["text"])["tier"] in ("B", "C"), row["text"]


# ----------------------------------------------- the result's own contract ----
def test_the_result_has_the_documented_shape_and_survives_json():
    allowed = list(worlds()["W5"])
    result = M.match_transcript(
        "please put the egg plant in the blew plate", allowed, family="all"
    )
    assert set(result) == {"tier", "flag", "family", "best", "alternatives", "allowed"}
    assert result["family"] == "all" and result["allowed"] == allowed
    best = result["best"]
    assert set(best) == {"prompt", "score", "fixes", "differences"}
    assert isinstance(best["score"], int) and 0 <= best["score"] <= 100
    assert best["fixes"] == [
        {"heard": "egg plant", "as": "eggplant"},
        {"heard": "blew", "as": "blue"},
    ]
    assert len(result["alternatives"]) == 2
    assert all(
        set(a) == {"prompt", "score", "differences"} for a in result["alternatives"]
    )
    assert json.loads(json.dumps(result)) == result


def test_differences_say_what_differs_in_the_documented_codes():
    def differences(text):
        return match(text, "W5")["best"]["differences"]

    assert differences("pick the eggplant in the red plate") == [
        {"code": "color_mismatch", "said": "red", "expected": "blue"}
    ]
    assert differences("pick the eggplant in the plate") == [
        {"code": "color_mismatch", "said": "", "expected": "blue"}
    ]
    assert differences("pick the eggplant") == [
        {"code": "missing_entity", "said": "", "expected": "plate"}
    ]
    assert differences("pick the eggplant in the blue plate and the tomato") == [
        {"code": "extra_entity", "said": "tomato", "expected": ""}
    ]
    assert differences("place tomato on red plate and eggplant on blue plate") == [
        {"code": "steps", "said": "1", "expected": "2"}
    ]
    assert differences("pick the eggplant in the blue plate quickly") == [
        {"code": "unknown_word", "said": "quickly", "expected": ""}
    ]
    swapped = differences(
        "stack white plate on red plate then place eggplant on blue plate"
    )
    assert {"code": "order", "said": "", "expected": ""} in swapped
    size = differences("pick screws of different size into the empty slot of screw box")
    assert {"code": "extra_entity", "said": "different", "expected": ""} in size
    assert {"code": "missing_entity", "said": "", "expected": "same"} in size
    size_word = differences(
        "pick screws of same color into the empty slot of screw box"
    )
    assert {"code": "size_mismatch", "said": "", "expected": "size"} in size_word


def test_every_prompt_in_a_result_is_an_element_of_the_allowed_list():
    allowed = list(worlds()["W53"])
    texts = [r["text"] for r in hand_corpus()] + [
        r["text"] for r in noise_corpus()[:200]
    ]
    for text in texts:
        result = M.match_transcript(text, allowed, family="all")
        assert set(shown(result)) <= set(allowed)
        assert result["allowed"] == allowed
        if text.strip() and len(text) < 60 and result["best"]:
            assert result["best"]["prompt"] != text or text in allowed


def test_the_transcript_never_appears_in_any_returned_prompt():
    allowed = [BASE, "stack the plates of same color together"]
    attack = f"{BASE} xyzzy {'q' * 200}"
    result = M.match_transcript(attack, allowed, family="restricted")
    assert result["tier"] == "B"
    for item in [result["best"], *result["alternatives"]]:
        assert item["prompt"] in allowed
    assert result["best"]["differences"][0]["said"] == "xyzzy"
    assert all(len(d["said"]) <= 40 for d in result["best"]["differences"])
    assert "q" * 41 not in json.dumps(result)


def test_tiers_c_and_v_name_no_instruction_and_a_and_b_offer_at_most_three():
    allowed = list(worlds()["W53"])
    assert M.match_transcript("tell me a joke", allowed, family="all")["best"] is None
    for text in ("pick the eggplant", BASE, "pick the eggplant in the plate"):
        result = M.match_transcript(text, allowed, family="all")
        assert len(shown(result)) == 3 and len(set(shown(result))) == 3


def test_family_is_echoed_and_checked():
    allowed = list(worlds()["W2"])
    assert (
        M.match_transcript(BASE, allowed, family="restricted")["family"] == "restricted"
    )
    with pytest.raises(ValueError):
        M.match_transcript(BASE, allowed, family="everything")
    with pytest.raises(TypeError):
        M.match_transcript(None, allowed, family="all")
    with pytest.raises(TypeError):
        M.match_transcript(b"pick", allowed, family="all")
    with pytest.raises(TypeError):
        M.match_transcript(BASE, "pick the eggplant in the blue plate", family="all")
    with pytest.raises(TypeError):
        M.match_transcript(BASE, [BASE, 3], family="all")


def test_an_empty_allowed_list_offers_nothing_and_a_repeated_one_is_collapsed():
    assert M.match_transcript(BASE, [], family="all")["tier"] == "C"
    assert M.match_transcript("stop", [], family="all")["tier"] == "V"
    twice = M.match_transcript(BASE, [BASE, BASE], family="all")
    assert (
        twice["tier"] == "A"
        and twice["allowed"] == [BASE]
        and twice["alternatives"] == []
    )


def test_the_result_does_not_depend_on_the_hash_seed():
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from levi.marvel0 import speech_match as M\n"
        "allowed = json.load(sys.stdin)\n"
        "texts = ['pick the egg plant in the blew plate', 'stack the blades of same color',"
        " 'stack white plate on red plate then place eggplant on blue plate',"
        " '把茄籽放到蓝盘子里', 'pick screws of different size into the slot',"
        " 'pick the eggplant in the green plate and the tomato']\n"
        "print(json.dumps([M.match_transcript(t, allowed, family='all') for t in texts]))\n"
    )
    outputs = set()
    for seed in ("0", "1", "4242"):
        done = subprocess.run(
            [sys.executable, "-c", script],
            input=json.dumps(list(worlds()["W53"])),
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            check=True,
            timeout=60,
        )
        outputs.add(done.stdout)
    assert len(outputs) == 1


# --------------------------------------------- the allowed list's invariants ----
@pytest.mark.parametrize("world", WORLD_NAMES)
def test_the_real_allowed_lists_pass_the_load_time_check(world):
    assert M.check_allowed_set(list(worlds()[world])) == []


def test_check_allowed_set_reports_a_word_the_lexicon_does_not_know():
    problems = M.check_allowed_set(
        [*worlds()["W5"], "pick the banana in the blue plate"]
    )
    assert len(problems) == 1 and "banana" in problems[0] and "I1" in problems[0]
    assert M.check_allowed_set(["pick the eggplant in the blue plate quickly"])


def test_check_allowed_set_reports_two_prompts_that_cannot_be_told_apart():
    twins = [BASE, "put the eggplant into the blue plate"]
    problems = M.check_allowed_set(twins)
    assert len(problems) == 1 and "I2" in problems[0] and twins[0] in problems[0]
    assert M.check_allowed_set([BASE, "pick the eggplant in the red plate"]) == []


def test_check_allowed_set_reports_prompts_that_could_never_be_selected():
    assert M.check_allowed_set(["stop the eggplant in the blue plate"])
    assert M.check_allowed_set(["pick the eggplant; in the blue plate"])
    assert M.check_allowed_set(["don't pick the eggplant"])
    assert M.check_allowed_set(["the of and"])
    assert M.check_allowed_set(["pick the eggplant " * 30])


def test_check_allowed_set_reports_a_malformed_list():
    assert M.check_allowed_set([])
    assert M.check_allowed_set(BASE)
    assert M.check_allowed_set([BASE, BASE])
    assert M.check_allowed_set([BASE, 7])


def test_a_prompt_with_a_word_the_matcher_cannot_see_is_never_pre_selected():
    """Invariant I1 in the matching path: 'quickly' is not in the lexicon, so a
    sentence without it would look perfect; the candidate is capped at tier B."""
    allowed = [
        "pick the eggplant in the blue plate quickly",
        "stack the plates of same color together",
    ]
    result = M.match_transcript(BASE, allowed, family="all")
    assert result["tier"] == "B" and chosen(result) == allowed[0]


def test_two_prompts_that_both_fit_are_not_pre_selected():
    """Invariant I2 in the matching path: with twins in the list, tier A would
    hide one of the two from the person."""
    twins = [
        BASE,
        "put the eggplant into the blue plate",
        "stack the plates of same color together",
    ]
    result = M.match_transcript(BASE, twins, family="all")
    assert result["tier"] == "B" and set(shown(result)[:2]) == set(twins[:2])


# ------------------------------------------------------------ the lexicon ----
def test_every_repair_target_is_a_lexicon_word():
    for heard, target in lexicon.EN_CONFUSIONS.items():
        assert M._lookup_english(target), (heard, target)
        assert M._lookup_english(heard) is None and not M._is_filler(heard), heard
    for heard, (concept, spelled) in lexicon.zh_repair_spellings().items():
        assert lexicon.ZH_CONCEPTS[spelled] == concept
        assert heard not in lexicon.ZH_CONCEPTS


def test_traditional_table_is_pairs_and_changes_only_characters_that_differ():
    pairs = lexicon._T2S_PAIRS
    assert len(pairs) % 2 == 0
    assert all(
        pairs[i] != pairs[i + 1] or pairs[i] in "面丝" for i in range(0, len(pairs), 2)
    )


def test_english_and_chinese_name_the_same_concepts():
    english = set(lexicon.EN_CONCEPTS.values())
    chinese = set(lexicon.ZH_CONCEPTS.values())
    assert english == chinese


def test_a_form_listed_under_two_concepts_is_refused_at_import():
    with pytest.raises(ValueError):
        lexicon._table((("N:A", ("x",)), ("N:B", ("x",))))


def test_the_chinese_repair_table_has_the_documented_example():
    assert lexicon.zh_repair_spellings()["茄籽"] == ("N:EGGPLANT", "茄子")
    assert lexicon.zh_repair_spellings()["兰"] == ("J:BLUE", "蓝")


# ------------------------------------------------- robustness and speed ----
def random_text(rnd):
    pools = [
        "abcdefghijklmnopqrstuvwxyz ", "ABC xyz ", "0123456789.-", "!\"#$%&'()*+,-./:;<=>?@[]^_`{|}~",
        "把茄子放到蓝盘子里然后再叠", "停别不", "あいア", "абв", "ال",
        "\t\n\r\x00\x1b\x7f", chars(0x200B, 0x200D, 0x202E, 0xFEFF, 0xAD, 0xFE0F),
        chars(0x301, 0x308), chars(0x1F916, 0x1F600), chars(0xD800, 0xDC00),
        chars(0x10FFFF, 0xE0041, 0xE000), chars(0x2028, 0x2029, 0xA0, 0x3000),
    ]  # fmt: skip
    return "".join(
        rnd.choice(rnd.choice(pools))
        for _ in range(rnd.choice([0, 1, 5, 30, 120, 450]))
    )


def test_match_transcript_never_raises_on_arbitrary_text():
    rnd = random.Random(20261020)
    allowed = list(worlds()["W53"])
    e_acute = chars(0xE9)
    specials = [
        "", " ", "\x00", ".", "'", "-", "--", "a" * 100_000, "停" * 100_000, "ab " * 200_000,
        chars(0xD800) * 10, chars(0x10FFFF), e_acute * 50, "9" * 399, (e_acute + chars(0x301)) * 300,
        "pick " * 100_000, BASE + "\x00" * 100_000,
    ]  # fmt: skip
    for text in specials + [random_text(rnd) for _ in range(4000)]:
        result = M.match_transcript(text, allowed, family="all")
        assert result["tier"] in ("A", "B", "C", "V")
        assert set(shown(result)) <= set(allowed)
        json.dumps(result, ensure_ascii=False).encode("utf-8")
    assert M.check_allowed_set([random_text(rnd) for _ in range(30)]) is not None


def test_the_median_call_is_fast_with_the_53_task_list():
    allowed = list(worlds()["W53"])
    texts = [r["text"] for r in hand_corpus()][:150]
    durations = []
    for text in texts:
        started = time.perf_counter()
        M.match_transcript(text, allowed, family="all")
        durations.append(time.perf_counter() - started)
    assert statistics.median(durations) < 0.002


def test_counts_by_tier_are_reported_for_the_record():
    """No assertion on the exact figures (they are tuned); this keeps the
    numbers in the test output when a run is read by a person."""
    for world in WORLD_NAMES:
        tiers = collections.Counter(
            (res["tier"], gold is not None) for _, gold, res in run("hand", world)
        )
        print(world, dict(sorted(tiers.items())))


@pytest.mark.parametrize(
    "text",
    [
        "take the eggplant off the blue plate",
        "move the eggplant away from the blue plate",
        "move the eggplant away into the blue plate",
        "move the eggplant over the blue plate",
        "把茄子从蓝盘子里拿出来",
        "把茄子拿走",
    ],
)
def test_direction_changes_are_never_preselected_as_placement(text):
    result = M.match_transcript(text, [BASE], family="all")
    assert result["tier"] != "A", result


def test_cut_it_out_vetoes_task_suggestions():
    result = M.match_transcript("cut it out", [BASE], family="all")
    assert result["tier"] == "V" and result["flag"] == "stop"
