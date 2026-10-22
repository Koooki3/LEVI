"""Match a spoken sentence to one of a closed list of task instructions.

The marvel0 page lets a person say the task instruction instead of typing it. A
speech recogniser gives text; this module decides which *allowed* instruction
(if any) the text means. The text itself never reaches a command: every
instruction in a result is an element of the ``allowed`` list, verbatim, and a
person still has to click it. So the module can only be wrong in one way that
matters, naming an instruction the person did not mean, and it is built to
prefer "I am not sure" to that.

How it works (standard library only, deterministic, no I/O):

1. Clean the text (NFKC, lower case, traditional -> simplified for the lexicon's
   characters, punctuation and filler words dropped).
2. Veto first: a stop word ("stop", "停", "别动"), a negation ("don't", "不要"),
   an unsafe character or flag-like token (``; | & $ ` \\ < > ( ) { } = "``,
   ``--x``, control or invisible characters) or text over 400 characters. A veto
   gives tier ``V`` and no instruction at all.
3. Read the sentence and each allowed instruction into the same structure of
   concepts (``speech_lexicon.py``): steps split at "then", and per step the
   action, the things with their colour or size, and the relations
   ("same", "from"). Chinese and English meet at the concepts.
4. Score 100 minus penalties for every difference (an unknown word, a missing
   or extra thing, a wrong colour, a changed order, a different number of
   steps, ...). A word *guessed* from a likely recognition mistake ("blew" for
   "blue", "egg plant", "茄籽") costs 12 points and is listed in ``fixes``, so a
   result with ``fixes`` can never reach tier A.
5. Tier ``A``: best score >= 90 (and no second instruction that high): one
   instruction is pre-selected. ``B``: 60-89: the best three are offered and
   none is pre-selected. ``C``: below 60: nothing fits, the whole list is
   offered. Measured on 375 hand-written sentences and 1,611 generated near
   misses against the five site instructions and a 53-instruction training set,
   no sentence was ever accepted at tier A for the wrong instruction.

Two properties of the allowed list make that work, and ``check_allowed_set``
tests them (call it when the list is loaded and refuse speech if it reports a
problem):

* I1: every word of every allowed instruction is in the lexicon. A word the
  matcher cannot see is a word a sentence could omit and still pass.
* I2: no two allowed instructions score >= 90 against each other.

To add an allowed instruction (the strings live in the site file, not here):
run ``check_allowed_set`` on the new list; add each word it reports to
``speech_lexicon.py`` (English in ``EN_CONCEPTS``, the Chinese name in
``ZH_CONCEPTS``, under an existing concept if the meaning is the same); add rows
for it to the fixtures ``tests/marvel0/fixtures/speech_*.json`` (its paraphrases,
a Chinese sentence, a near miss that must not match) and run
``tests/marvel0/test_marvel0_speech_match.py``: the zero-wrong tests must stay green.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from typing import Final, Literal, TypedDict

from . import speech_lexicon as lexicon

__all__ = ["check_allowed_set", "match_transcript"]

FAMILIES: Final = ("restricted", "all")
Family = Literal["restricted", "all"]
Tier = Literal["A", "B", "C", "V"]
Flag = Literal["stop", "negation", "unsafe"]
Note = Callable[[str, str, str], None]

MAX_TRANSCRIPT_CHARS: Final = 400
TIER_A_MIN: Final = 90
TIER_B_MIN: Final = 60
# At most this many offered instructions beside the best one.
ALTERNATIVES: Final = 2
_SHOWN_LEN: Final = 40
_SHOWN_UNKNOWN: Final = 5

# Points taken off 100 for each kind of difference (see the module docstring).
PENALTY: Final[dict[str, int]] = {
    "unknown": 20,  # per word the lexicon does not know ...
    "unknown_cap": 60,  # ... at most this many points in all
    "repair": 12,  # a guessed word: keeps the result below tier A
    "steps": 30,
    "action_missing": 12,
    "action_conflict": 15,
    "modifier_omitted": 20,
    "modifier_conflict": 35,
    "thing_missing": 25,
    "thing_extra": 25,
    "order": 20,
    "relation": 15,
    "loose_modifier": 20,
}
_RELATION_LIGHT: Final = 3  # "together" present or absent barely changes the task

_REPAIRED: Final = frozenset({"alias", "joined", "fuzzy", "homophone"})


class Difference(TypedDict):
    code: str
    said: str
    expected: str


class Alternative(TypedDict):
    prompt: str
    score: int
    differences: list[Difference]


class Best(Alternative):
    fixes: list[dict[str, str]]


class MatchResult(TypedDict):
    tier: Tier
    flag: Flag | None
    family: Family
    best: Best | None
    alternatives: list[Alternative]
    allowed: list[str]


# ------------------------------------------------------------ text cleaning ----
_HIDDEN_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# Ranges are built with chr(): an escape sequence in a source file can be turned
# into the (invisible) character by tools, and then nobody can read the pattern.
_CJK_RANGE: Final = f"{chr(0x3400)}-{chr(0x9FFF)}"
_VARIATION_SELECTORS: Final = re.compile(
    f"[{chr(0xFE00)}-{chr(0xFE0F)}{chr(0x180B)}-{chr(0x180D)}{chr(0xE0100)}-{chr(0xE01EF)}]"
)
_CURLY_APOSTROPHE: Final = re.compile(r"(?<=[a-z])’(?=[a-z])")
_UNSAFE: Final = re.compile(
    r"[;|&$`\\<>(){}\[\]*#~=\"‘’“”„‟«»"
    r"「」『』《》【】〔〕〈〉]"
    r"|--|\.\./|(?:^|\s)-\w|(?<![a-z])'|'(?![a-z])"
)
_NOT_WORD: Final = re.compile(r"[^\w\s']|_")
_TOKEN: Final = re.compile(
    r"[a-z]+(?:'[a-z]+)?"  # an English word, contraction included
    rf"|[{_CJK_RANGE}]+"  # a run of Chinese characters
    r"|\d+(?:\.\d+)?"
    r"|[^\W\d_]+"  # letters of any other script: never a task word
)
_ENGLISH_WORD: Final = re.compile(r"[a-z]+")
_CJK: Final = re.compile(f"[{_CJK_RANGE}]+")


@dataclass(frozen=True)
class _Text:
    words: str
    unsafe: bool


def _drop_hidden(text: str) -> tuple[str, bool]:
    """``text`` without control and invisible characters, and whether any was there.

    A space-like character other than the plain space becomes a plain space and
    does not count: it is how some recognisers separate words.
    """
    kept: list[str] = []
    found = False
    for ch in text:
        category = unicodedata.category(ch)
        if category == "Zs":
            kept.append(" ")
        elif category in ("Cc", "Zl", "Zp"):
            kept.append(" ")
            found = True
        elif category in _HIDDEN_CATEGORIES or _VARIATION_SELECTORS.match(ch):
            found = True
        else:
            kept.append(ch)
    return "".join(kept), found


def _clean(raw: str) -> _Text:
    """The text to read, and whether it holds anything a task never would."""
    if len(raw) > MAX_TRANSCRIPT_CHARS:
        return _Text("", True)
    text = unicodedata.normalize("NFKC", raw).strip()
    if len(text) > MAX_TRANSCRIPT_CHARS:
        return _Text("", True)
    unsafe = False
    if not text.isprintable() or _VARIATION_SELECTORS.search(text):
        text, unsafe = _drop_hidden(text)
    text = text.lower().translate(lexicon.TRADITIONAL_TO_SIMPLIFIED)
    text = _CURLY_APOSTROPHE.sub("'", text)
    unsafe = unsafe or _UNSAFE.search(text) is not None
    return _Text(_NOT_WORD.sub(" ", text), unsafe)


# ------------------------------------------------------------------ lexicon ----
_EN_ONE: Final = {k: v for k, v in lexicon.EN_CONCEPTS.items() if " " not in k}
_EN_MANY: Final = {k: v for k, v in lexicon.EN_CONCEPTS.items() if " " in k}
_EN_MANY_LEN: Final = max(len(k.split()) for k in _EN_MANY)
_ZH_LENGTHS: Final = sorted({len(k) for k in lexicon.ZH_CONCEPTS}, reverse=True)
_ZH_ALIASES: Final = lexicon.zh_repair_spellings()
_ZH_ALIAS_LENGTHS: Final = sorted({len(k) for k in _ZH_ALIASES}, reverse=True)
_VETO: Final = frozenset({"NEG", "STOP"})


def _index_fuzzy_keys() -> dict[str, list[str]]:
    """Lexicon words a typo may be repaired to, by first letter (never a veto word)."""
    index: dict[str, list[str]] = {}
    for key, concept in _EN_ONE.items():
        if len(key) >= 4 and concept not in _VETO:
            index.setdefault(key[0], []).append(key)
    return index


_FUZZY_KEYS: Final = _index_fuzzy_keys()
_SUFFIXES: Final = (
    ("ies", "y"), ("es", ""), ("s", ""), ("ed", ""), ("ed", "e"),
    ("ing", ""), ("ing", "e"), ("ly", ""), ("er", ""),
)  # fmt: skip


@lru_cache(maxsize=4096)
def _stems(word: str) -> tuple[str, ...]:
    """The plausible base forms of an inflected English ``word``."""
    out: list[str] = []
    if word.endswith("'s"):
        out.append(word[:-2])
    for suffix, replacement in _SUFFIXES:
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            base = word[: -len(suffix)] + replacement
            out.append(base)
            if len(base) > 3 and base[-1] == base[-2]:
                out.append(base[:-1])  # putt -> put
    return tuple(out)


def _lookup_english(word: str) -> tuple[str, str] | None:
    """(concept, how) for an English word the lexicon knows, else ``None``."""
    concept = _EN_ONE.get(word)
    if concept:
        return concept, "exact"
    for stem in _stems(word):
        concept = _EN_ONE.get(stem)
        if concept:
            return concept, "stem"
    return None


def _is_filler(word: str) -> bool:
    return word in lexicon.FILLERS_EN or any(
        s in lexicon.FILLERS_EN for s in _stems(word)
    )


def _distance(a: str, b: str) -> int:
    """Levenshtein distance."""
    if len(a) < len(b):
        a, b = b, a
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        diagonal, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            diagonal, row[j] = (
                row[j],
                min(row[j] + 1, row[j - 1] + 1, diagonal + (ca != cb)),
            )
    return row[-1]


def _similarity(a: str, b: str) -> Fraction:
    return 1 - Fraction(_distance(a, b), max(len(a), len(b)))


def _fuzzy_word(word: str) -> str | None:
    """The one lexicon word a misspelt ``word`` is a typo of, if it is clear.

    The first letter must agree, the lengths differ by at most two, the
    similarity is at least 0.8, and the runner-up (if of another concept) is
    clearly behind.
    """
    if len(word) < 4:
        return None
    ranked = sorted(
        (
            (_similarity(key, word), key)
            for key in _FUZZY_KEYS.get(word[0], ())
            if abs(len(key) - len(word)) <= 2
        ),
        reverse=True,
    )[:2]
    if not ranked or ranked[0][0] < Fraction(4, 5):
        return None
    if len(ranked) == 2:
        (best_sim, best), (next_sim, other) = ranked
        if best_sim - next_sim < Fraction(1, 20) and _EN_ONE[best] != _EN_ONE[other]:
            return None
    return ranked[0][1]


def _fuzzy_joined(joined: str) -> str | None:
    """A lexicon word two adjacent tokens misspell together ("egg plan")."""
    ranked = sorted(
        (_similarity(key, joined), key) for key in _FUZZY_KEYS.get(joined[0], ())
    )
    if ranked and ranked[-1][0] >= Fraction(17, 20):
        return ranked[-1][1]
    return None


# ------------------------------------------------------------------ parsing ----
@dataclass(frozen=True)
class _Tok:
    concept: str
    via: str  # exact | stem | alias | joined | fuzzy | homophone
    heard: str
    spelled: str  # the lexicon spelling (what a repair read it as)

    @property
    def repaired(self) -> bool:
        return self.via in _REPAIRED


@dataclass(frozen=True)
class _Clause:
    acts: frozenset[str]
    things: tuple[tuple[frozenset[str], str], ...]  # (modifiers, noun), in order
    relations: frozenset[str]
    loose: frozenset[str]  # modifiers with no noun


@dataclass(frozen=True)
class _Parsed:
    tokens: tuple[_Tok, ...]
    unknown: tuple[str, ...]
    unknown_units: int
    clauses: tuple[_Clause, ...]
    stop: bool
    neg: bool


class _Reader:
    """Collects the concepts of one sentence."""

    def __init__(self, repair: bool) -> None:
        self.repair = repair
        self.tokens: list[_Tok] = []
        self.unknown: list[str] = []
        self.units = 0

    def miss(self, text: str, units: int = 1) -> None:
        self.unknown.append(text)
        self.units += units

    def english(self, words: list[str], at: int) -> int:
        """Read the English word at ``at``; return the index after what was read."""
        word = words[at]
        if not _ENGLISH_WORD.fullmatch(word.replace("'", "")):
            self.miss(word)
            return at + 1
        for size in range(min(_EN_MANY_LEN, len(words) - at), 1, -1):
            phrase = " ".join(words[at : at + size])
            if phrase in _EN_MANY:
                self.tokens.append(_Tok(_EN_MANY[phrase], "exact", phrase, phrase))
                return at + size
        found = _lookup_english(word)
        if found:
            self.tokens.append(_Tok(found[0], found[1], word, word))
            return at + 1
        if _is_filler(word):
            return at + 1
        if self.repair:
            step = self._repair_english(words, at)
            if step:
                return at + step
        self.miss(word)
        return at + 1

    def _repair_english(self, words: list[str], at: int) -> int:
        """Guess what an unknown word was meant to be; 0 when it is not clear."""
        word = words[at]
        target = lexicon.EN_CONFUSIONS.get(word)
        if target and self._guess(target, "alias", word):
            return 1
        following = words[at + 1] if at + 1 < len(words) else ""
        if _ENGLISH_WORD.fullmatch(following):
            joined = word + following
            both = f"{word} {following}"
            if _lookup_english(joined) and self._guess(joined, "joined", both):
                return 2
            key = _fuzzy_joined(joined) if len(joined) >= 6 else None
            if key and self._guess(key, "joined", both):
                return 2
        key = _fuzzy_word(word)
        if key and self._guess(key, "fuzzy", word):
            return 1
        return 0

    def _guess(self, spelled: str, via: str, heard: str) -> bool:
        found = _lookup_english(spelled)
        if found:  # the repair tables only point at lexicon words
            self.tokens.append(_Tok(found[0], via, heard, spelled))
        return found is not None

    def chinese(self, run: str) -> None:
        gap: list[str] = []
        at = 0
        while at < len(run):
            token = self._chinese_token(run, at)
            if token:
                self._flush(gap)
                gap = []
                self.tokens.append(token[0])
                at += token[1]
            else:
                if run[at] not in lexicon.FILLERS_ZH:
                    gap.append(run[at])
                at += 1
        self._flush(gap)

    def _flush(self, gap: list[str]) -> None:
        if gap:
            self.miss("".join(gap), len(gap))

    def _chinese_token(self, run: str, at: int) -> tuple[_Tok, int] | None:
        exact = next(
            (
                run[at : at + n]
                for n in _ZH_LENGTHS
                if run[at : at + n] in lexicon.ZH_CONCEPTS
            ),
            None,
        )
        if self.repair:
            # A respelling longer than the plain word wins: 盘字 is 盘子, not 盘 + 字.
            for n in _ZH_ALIAS_LENGTHS:
                window = run[at : at + n]
                if window in _ZH_ALIASES and n > len(exact or ""):
                    concept, spelled = _ZH_ALIASES[window]
                    return _Tok(concept, "homophone", window, spelled), n
        if exact:
            return _Tok(lexicon.ZH_CONCEPTS[exact], "exact", exact, exact), len(exact)
        return None


def _clauses(tokens: Sequence[_Tok]) -> tuple[_Clause, ...]:
    steps: list[list[_Tok]] = []
    current: list[_Tok] = []
    for tok in tokens:
        if tok.concept == "T":
            if current:
                steps.append(current)
            current = []
        else:
            current.append(tok)
    if current or not steps:
        steps.append(current)
    return tuple(_clause(step) for step in steps)


def _clause(step: Sequence[_Tok]) -> _Clause:
    acts: set[str] = set()
    relations: set[str] = set()
    things: list[tuple[frozenset[str], str]] = []
    pending: set[str] = set()
    for tok in step:
        kind = tok.concept[0]
        if kind == "A":
            acts.add(tok.concept)
        elif kind == "R":
            relations.add(tok.concept)
        elif kind == "J":
            pending.add(tok.concept)
        elif kind == "N":
            things.append((frozenset(pending), tok.concept))
            pending = set()
    loose: frozenset[str] = frozenset()
    if pending:
        if things:
            things[-1] = (things[-1][0] | frozenset(pending), things[-1][1])
        else:
            loose = frozenset(pending)
    return _Clause(frozenset(acts), tuple(things), frozenset(relations), loose)


def _parse(words: str, *, repair: bool) -> _Parsed:
    reader = _Reader(repair)
    pieces = _TOKEN.findall(words)
    at = 0
    while at < len(pieces):
        piece = pieces[at]
        if _CJK.fullmatch(piece):
            reader.chinese(piece)
            at += 1
        elif piece[0].isdigit():
            reader.miss(piece)
            at += 1
        else:
            at = reader.english(pieces, at)
    concepts = [t.concept for t in reader.tokens]
    content = tuple(t for t in reader.tokens if t.concept not in _VETO)
    return _Parsed(
        content,
        tuple(reader.unknown),
        reader.units,
        _clauses(content),
        "STOP" in concepts,
        "NEG" in concepts,
    )


@lru_cache(maxsize=1024)
def _candidate(prompt: str) -> tuple[_Text, _Parsed]:
    """An allowed instruction read strictly: no guessing about its words."""
    text = _clean(prompt)
    return text, _parse(text.words, repair=False)


def _flag(text: _Text, parsed: _Parsed) -> Flag | None:
    if parsed.stop:
        return "stop"
    if text.unsafe:
        return "unsafe"
    if parsed.neg:
        return "negation"
    return None


# ------------------------------------------------------------------ scoring ----
def _clip(text: str) -> str:
    return text if len(text) <= _SHOWN_LEN else text[: _SHOWN_LEN - 1] + "…"


def _names(concepts: frozenset[str] | set[str]) -> str:
    return ", ".join(sorted(lexicon.label(c) for c in concepts))


def _modifier_code(modifiers: frozenset[str], candidate_has: bool) -> str:
    if modifiers & lexicon.COLOR_MODIFIERS:
        return "color_mismatch"
    if modifiers & lexicon.SIZE_MODIFIERS:
        return "size_mismatch"
    return "missing_entity" if candidate_has else "extra_entity"


def _merge(clauses: Sequence[_Clause]) -> _Clause:
    return _Clause(
        frozenset().union(*(c.acts for c in clauses)),
        tuple(thing for c in clauses for thing in c.things),
        frozenset().union(*(c.relations for c in clauses)),
        frozenset().union(*(c.loose for c in clauses)),
    )


def _longest_in_order(sequence: list[int]) -> int:
    """Length of the longest increasing subsequence (quadratic; sequences are tiny)."""
    best = [1] * len(sequence)
    for i, value in enumerate(sequence):
        for j in range(i):
            if sequence[j] < value:
                best[i] = max(best[i], best[j] + 1)
    return max(best, default=0)


def _score(
    said: _Parsed, wanted: _Parsed, notes: list[Difference] | None = None
) -> int:
    """How well ``said`` matches ``wanted`` (0-100); ``notes`` collects the reasons."""

    def note(code: str, heard: str, expected: str) -> None:
        if notes is not None:
            notes.append(
                {"code": code, "said": _clip(heard), "expected": _clip(expected)}
            )

    if not said.tokens:
        return 0
    total = 0
    if said.unknown_units:
        total += min(PENALTY["unknown_cap"], PENALTY["unknown"] * said.unknown_units)
        for word in said.unknown[:_SHOWN_UNKNOWN]:
            note("unknown_word", word, "")
    total += PENALTY["repair"] * sum(tok.repaired for tok in said.tokens)
    if len(said.clauses) != len(wanted.clauses):
        total += PENALTY["steps"]
        note("steps", str(len(said.clauses)), str(len(wanted.clauses)))
        pairs = [(_merge(said.clauses), _merge(wanted.clauses))]
    else:
        pairs = list(zip(said.clauses, wanted.clauses))
    for heard, expected in pairs:
        total += _score_step(heard, expected, note)
    return max(0, 100 - total)


def _score_step(heard: _Clause, expected: _Clause, note: Note) -> int:
    total = 0
    # pick / put / place are one generic action; a specific one (stack, pour) must agree.
    specific_heard = heard.acts - {"A:MOVE"}
    specific_expected = expected.acts - {"A:MOVE"}
    if not heard.acts:
        if expected.acts:
            total += PENALTY["action_missing"]
            note("action", "", _names(expected.acts))
    elif specific_heard != specific_expected:
        total += PENALTY["action_conflict"]
        note("action", _names(heard.acts), _names(expected.acts))

    things_heard, things_expected = list(heard.things), list(expected.things)
    used_heard: set[int] = set()
    used_expected: set[int] = set()
    order_heard: list[int] = []
    for ci, want in enumerate(things_expected):  # identical thing (same modifiers)
        for ti, got in enumerate(things_heard):
            if ti not in used_heard and got == want:
                used_heard.add(ti)
                used_expected.add(ci)
                order_heard.append(ti)
                break
    for ci, want in enumerate(things_expected):  # same noun, other modifiers
        if ci in used_expected:
            continue
        for ti, got in enumerate(things_heard):
            if ti not in used_heard and got[1] == want[1]:
                used_heard.add(ti)
                used_expected.add(ci)
                omitted = got[0] < want[0]
                total += PENALTY["modifier_omitted" if omitted else "modifier_conflict"]
                differing = want[0] - got[0] if omitted else got[0] ^ want[0]
                note(
                    _modifier_code(differing, bool(want[0] - got[0])),
                    _names(got[0]),
                    _names(want[0]),
                )
                break
    for ci, want in enumerate(things_expected):
        if ci not in used_expected:
            total += PENALTY["thing_missing"]
            note("missing_entity", "", lexicon.label(want[1]))
    for ti, got in enumerate(things_heard):
        if ti not in used_heard:
            total += PENALTY["thing_extra"]
            note("extra_entity", lexicon.label(got[1]), "")
    if len(order_heard) > 1 and _longest_in_order(order_heard) != len(order_heard):
        total += PENALTY["order"]
        note("order", "", "")
    for relation in sorted(heard.relations ^ expected.relations):
        extra = relation in heard.relations
        if relation == "R:TOGETHER":
            total += _RELATION_LIGHT
            continue
        total += PENALTY["relation"]
        code = {"R:COLOR": "color_mismatch", "R:SIZE": "size_mismatch"}.get(
            relation, "extra_entity" if extra else "missing_entity"
        )
        word = lexicon.label(relation)
        note(code, word if extra else "", "" if extra else word)
    if heard.loose:
        total += PENALTY["loose_modifier"]
        note("extra_entity", _names(heard.loose), "")
    return total


# --------------------------------------------------------------- public API ----
def _prompt_list(allowed: Sequence[str]) -> list[str]:
    if isinstance(allowed, str):
        raise TypeError("allowed must be a sequence of strings, not one string")
    prompts = list(dict.fromkeys(allowed))
    for prompt in prompts:
        if not isinstance(prompt, str):
            raise TypeError(f"allowed holds a non-string: {type(prompt).__name__}")
    return prompts


def _fixes(parsed: _Parsed) -> list[dict[str, str]]:
    seen: dict[tuple[str, str], None] = {}
    for tok in parsed.tokens:
        if tok.repaired:
            seen[(_clip(tok.heard), tok.spelled)] = None
    return [{"heard": heard, "as": spelled} for heard, spelled in seen]


def _result(
    tier: Tier,
    flag: Flag | None,
    family: Family,
    best: Best | None,
    alternatives: list[Alternative],
    allowed: list[str],
) -> MatchResult:
    return {
        "tier": tier,
        "flag": flag,
        "family": family,
        "best": best,
        "alternatives": alternatives,
        "allowed": list(allowed),
    }


def match_transcript(
    transcript: str, allowed: Sequence[str], *, family: Family
) -> MatchResult:
    """Which of ``allowed`` does ``transcript`` mean?

    Returns the match object of the speech contract: ``tier`` (A pre-selected,
    B choose among three, C nothing fits, V vetoed), ``flag`` (``stop``,
    ``negation``, ``unsafe`` or ``None``), ``family``, ``best``
    (``prompt`` / ``score`` / ``fixes`` / ``differences``, ``None`` at tiers C
    and V), ``alternatives`` (the next two, tiers A and B) and ``allowed`` (the
    whole list, verbatim). Every ``prompt`` is an element of ``allowed``; the
    transcript is never returned as one. Never raises on any string.
    """
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {FAMILIES}, not {family!r}")
    if not isinstance(transcript, str):
        raise TypeError("transcript must be a string")
    prompts = _prompt_list(allowed)

    text = _clean(transcript)
    parsed = _parse(text.words, repair=True)
    flag = _flag(text, parsed)
    if flag:
        return _result("V", flag, family, None, [], prompts)

    scored: list[tuple[int, int]] = []
    for index, prompt in enumerate(prompts):
        wanted = _candidate(prompt)[1]
        score = _score(parsed, wanted)
        if wanted.unknown_units:  # a word the matcher cannot see: never pre-select
            score = min(score, TIER_A_MIN - 1)
        scored.append((-score, index))
    scored.sort()
    shown = [(-neg, index) for neg, index in scored[: 1 + ALTERNATIVES]]
    if not shown or shown[0][0] < TIER_B_MIN:
        return _result("C", None, family, None, [], prompts)

    def explain(score: int, index: int) -> Alternative:
        notes: list[Difference] = []
        _score(parsed, _candidate(prompts[index])[1], notes)
        return {"prompt": prompts[index], "score": score, "differences": notes}

    first = explain(*shown[0])
    best: Best = {**first, "fixes": _fixes(parsed)}
    alternatives = [explain(*pair) for pair in shown[1:]]
    sole = len(shown) < 2 or shown[1][0] < TIER_A_MIN
    tier: Tier = "A" if shown[0][0] >= TIER_A_MIN and sole else "B"
    return _result(tier, None, family, best, alternatives, prompts)


def _signature(prompt: str) -> tuple[_Clause, ...]:
    """The steps of ``prompt`` as concepts (tests use it to find equal meanings)."""
    return _candidate(prompt)[1].clauses


def check_allowed_set(allowed: Sequence[str]) -> list[str]:
    """Problems with an allowed list, as sentences; an empty list means fine.

    I1: every word of every instruction is in the lexicon. I2: no two score
    >= 90 against each other. Also reported: an empty or duplicated list, an
    instruction that holds a veto word or unsafe character (it could never be
    chosen) or no task word at all.
    """
    problems: list[str] = []
    if isinstance(allowed, str):
        return ["the allowed list is a single string, not a list of instructions"]
    entries = list(allowed)
    if not entries:
        return ["the allowed list is empty"]
    prompts: list[str] = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, str):
            problems.append(f"entry {position + 1} is not a string")
        elif entry in prompts:
            problems.append(f"{entry!r} is listed twice")
        else:
            prompts.append(entry)
    for prompt in prompts:
        text, parsed = _candidate(prompt)
        flag = _flag(text, parsed)
        if flag:
            problems.append(
                f"{prompt!r} holds a {flag} word or character, so it could never be selected"
            )
        if parsed.unknown:
            problems.append(
                f"{prompt!r}: not in the lexicon: {', '.join(parsed.unknown)}"
                " (invariant I1: add them to speech_lexicon.py)"
            )
        if not parsed.tokens:
            problems.append(f"{prompt!r} has no task word the matcher knows")
    for i, first in enumerate(prompts):
        for second in prompts[i + 1 :]:
            a, b = _candidate(first)[1], _candidate(second)[1]
            score = max(_score(a, b), _score(b, a))
            if score >= TIER_A_MIN:
                problems.append(
                    f"{first!r} and {second!r} score {score} against each other"
                    " (invariant I2: the matcher could not tell them apart)"
                )
    return problems
