"""Whether a machine translation of a Chinese object name may go into the voice
card's generated sentence.

``speech_template.generate`` leaves a sentence out when an object was named in
Chinese outside the lexicon. The speech worker's text model
(``integrations/stt/levi_stt_worker/translate.py``) gives three answers per name:
the English, that English translated back into Chinese, and whether the two
name the same kind of object. This module decides, with the standard library
only, which answers may be used:

1. **An object name.** One to three plain English words that pass the template's
   own filter (``speech_template._english_name``), and that the matcher reads
   without a stop or negation phrase ("hold on"), an action, a step separator
   ("followed by") or a relation; no place word ("on", "into"), conjunction
   ("and", "with"), direction or manner word, and not only a colour or a size.
   ``NONE`` (the model's "not an object") fails here.
2. **Not an object the lexicon already knows** (plurals included). The name was
   not in the lexicon, so an English that is a lexicon thing ("orange" for 金桔,
   "pencil" for 蜡笔) names an object the lexicon knows under other Chinese
   names: refused, and the page shows those names (``known_as``).
3. **Checked.** The back-translation is the same name (after NFKC, traditional
   -> simplified, 桔 = 橘, a trailing 子/儿/头 dropped). A back-translation that
   is only part of the name said (牙刷架, a toothbrush holder, back as 牙刷) is
   refused: the translation dropped what made it a different object. One that
   holds the whole name and more (乐高 back as 乐高积木), or that shares at
   least half of the shorter one's characters (花椰菜 and 花菜), passes only
   when the model also judges the two the same object. Otherwise the
   translation is ``unchecked`` and refused.

Measured on 2026-10-31 with Qwen3-4B-Instruct-2507 (levi-hub
programs/rig-console/reviews/voice-translate-20261031.md): on 81 names outside
the lexicon 69 passed and 2 of those were near misses ("honeydew" for 哈密瓜,
"cup pad" for 杯垫); on a held-out set of 40, frozen before it was run, 31
passed and none was wrong (3 were usable but not the usual word). None of 11
words that are not objects passed. A translation is still only a proposal: the
generated sentence is labelled, shows each translation and is used only when a
person presses its Use.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

from . import speech_lexicon as lexicon
from . import speech_match as matcher
from . import speech_template as template

Reason = Literal["not_object", "known_as", "unchecked"]

_VARIANTS: Final = str.maketrans({"桔": "橘", "菓": "果"})
# Words that join or place things, never part of one object's name.
_NOT_IN_NAME: Final = frozenset(
    [
        "and",
        "or",
        "with",
        "plus",
        "beside",
        "besides",
        "next",
        "near",
        "between",
        "then",
    ]
) | frozenset(template._PLACES)
_SUFFIXES: Final = "子儿头"
_ZH_FORMS: Final[dict[str, tuple[str, ...]]] = {}
for _form, _concept in lexicon.ZH_CONCEPTS.items():
    _ZH_FORMS[_concept] = (*_ZH_FORMS.get(_concept, ()), _form)


@dataclass(frozen=True)
class Verdict:
    """What the page is told about one name. ``english`` is the name to use when
    ``reason`` is None, else the model's answer (for display, or None);
    ``back`` the back-translation; ``check`` how it passed; ``known_as`` the
    lexicon's Chinese names when ``reason`` is ``known_as``."""

    said: str
    english: str | None
    back: str | None
    check: Literal["", "back_translation", "back_translation_and_judgement"]
    reason: Reason | None
    known_as: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.reason is None


def object_name(answer: str | None) -> str | None:
    """Rule 1: the answer as an object name, or None."""
    if not isinstance(answer, str) or answer.strip().upper() == "NONE":
        return None
    name = template._english_name(answer)
    if name is None:
        return None
    words = name.split(" ")
    if any(
        word in template._NOT_NAMES or word in _NOT_IN_NAME or word.endswith("ly")
        for word in words
    ):
        return None
    parsed = matcher._parse(name, repair=False)
    if (
        parsed.stop
        or parsed.neg
        or any(
            tok.concept == "T" or tok.concept.startswith(("A:", "R:"))
            for tok in parsed.tokens
        )
    ):
        return None  # "hold on", "followed by", "pick up"
    concepts = [matcher._lookup_english(word) for word in words]
    if all(found is not None and found[0].startswith("J:") for found in concepts):
        return None  # only a colour or a size
    return name


def known_concept(name: str) -> str | None:
    """Rule 2: the lexicon thing an English name is (any plural form), or None."""
    words = name.split(" ")
    forms = [name]
    if len(words) == 1:
        found = matcher._lookup_english(name)
        forms.append(found[0] if found else "")
    else:
        forms += [" ".join([*words[:-1], stem]) for stem in matcher._stems(words[-1])]
    for form in forms:
        concept = form if form.startswith("N:") else lexicon.EN_CONCEPTS.get(form)
        if concept and concept.startswith("N:"):
            return concept
    return None


def _norm(text: str | None) -> str:
    text = text if isinstance(text, str) else ""
    text = unicodedata.normalize("NFKC", text).translate(
        lexicon.TRADITIONAL_TO_SIMPLIFIED
    )
    return "".join(ch for ch in text.translate(_VARIANTS) if matcher._CJK.fullmatch(ch))


def _strip(text: str) -> str:
    return text[:-1] if len(text) >= 2 and text[-1] in _SUFFIXES else text


def back_matches(said: str, back: str | None) -> bool:
    """Rule 3, first part: the back-translation is the same name."""
    z, b = _norm(said), _norm(back)
    return bool(z and b) and (z == b or _strip(z) == _strip(b))


def back_is_part(said: str, back: str | None) -> bool:
    """The back-translation is only part of the name said (牙刷架 -> 牙刷)."""
    z, b = _norm(said), _norm(back)
    return bool(b) and len(b) < len(z) and b in z


def shared_half(said: str, back: str | None) -> bool:
    """Rule 3, second part: the two share at least half of the shorter one's
    characters (the model's judgement is then the deciding vote)."""
    z, b = set(_norm(said)), set(_norm(back))
    return bool(z and b) and len(z & b) / min(len(z), len(b)) >= 0.5


def judge(
    said: str, english: str | None, chinese: str | None, same: bool | None
) -> Verdict:
    """The verdict on the worker's answers for one name."""
    name = object_name(english)
    if name is None:
        return Verdict(said, None, None, "", "not_object")
    concept = known_concept(name)
    if concept is not None:
        return Verdict(said, name, None, "", "known_as", _ZH_FORMS.get(concept, ()))
    if back_matches(said, chinese):
        return Verdict(said, name, chinese, "back_translation", None)
    if back_is_part(said, chinese):
        return Verdict(said, name, chinese, "", "unchecked")
    if same is True and shared_half(said, chinese):
        return Verdict(said, name, chinese, "back_translation_and_judgement", None)
    return Verdict(said, name, chinese, "", "unchecked")
