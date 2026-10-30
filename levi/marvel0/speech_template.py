"""Generate a task instruction from the most common phrasing in the datasets.

The matcher (``speech_match``) only ever returns a sentence of the allowed
list. This module writes a new English sentence instead: it reads the action
and the objects of what was said (English or Chinese, through the same
lexicon) and puts them into the sentence frame the registered datasets use
most for that action, for example ``pick {A} into {B}``.

Rules that keep it safe to show (owner decision 2026-10-30, DECISIONS.md):

* a generated sentence is always labelled as generated, never as a known
  instruction, and only a person's click puts it into the field;
* nothing is generated for a stop, negation or unsafe text (the matcher's
  veto), nor for the restricted model family (only its two trained sentences);
* every slot is a lexicon word, an English word that was said, or a
  translation supplied by the caller; a slot that cannot be filled leaves the
  sentence out (``status`` says why) instead of guessing;
* the sentence is read back with the strict matcher reader: it must mean the
  same steps as what was heard, or it is not offered.

Standard library only; the dataset sentences are passed in by the caller.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, TypedDict

from . import speech_lexicon as lexicon
from . import speech_match as matcher

Status = Literal[
    "ok",  # a sentence is offered
    "vetoed",  # stop, negation or unsafe text: nothing is offered
    "restricted",  # the model only knows its trained sentences
    "no_task",  # no action with an object and a place was heard
    "unsupported",  # a relation (same, corresponding, from ...) a frame cannot hold
    "no_frame",  # the datasets have no frame for this action
    "untranslated",  # an object name has no English yet
    "unclear",  # a word was heard that no slot can hold (a number, another script)
]

# A step's destination preposition, as the datasets write it.
_PLACES: Final = ("into", "onto", "on", "in", "inside", "to")
_ARTICLES: Final = frozenset({"the", "a", "an"})
# Words that make a dataset step more than "verb A prep B": not a frame.
_NOT_SIMPLE: Final = frozenset(
    {"and", "of", "from", "same", "corresponding", "together", "it", "them", "all"}
)
_RESET: Final = re.compile(r"^\s*reset\s*:\s*", re.IGNORECASE)
_STEP_SPLIT: Final = re.compile(r"\s*(?:,\s*)?\b(?:and\s+)?then\b\s*")
_WORD: Final = re.compile(r"[a-z]+(?:-[a-z]+)*")
# Lexicon order of modifiers in an object phrase: size, then colour, then rest.
_SIZE_FIRST: Final = ("J:BIG", "J:SMALL")


@dataclass(frozen=True)
class Frame:
    action: str  # lexicon action concept, e.g. "A:MOVE"
    verb: str
    place: str  # the preposition before B
    the_a: bool
    the_b: bool
    episodes: int  # episodes whose instruction uses this frame
    example: str  # the most common instruction with this frame

    def pattern(self) -> str:
        a = "the {A}" if self.the_a else "{A}"
        b = "the {B}" if self.the_b else "{B}"
        return f"{self.verb} {a} {self.place} {b}"


@dataclass(frozen=True)
class Frames:
    by_action: Mapping[str, Frame]
    # The preposition the datasets use most before a destination, keyed by
    # "<action> <noun>" and by "<noun>" (lexicon concepts): "into" a bowl,
    # "on" a plate, "on" a cup that is stacked on but "into" one poured into.
    place_for: Mapping[str, str]
    # Dataset instructions exactly as written, with episodes: only task
    # instructions (no "Reset:"), and only ones the matcher reads cleanly (no
    # stop, negation, unsafe mark or unknown word). The dataset original
    # offered beside a generated sentence is one of these.
    sentences: Mapping[str, int]


class Slot(TypedDict):
    said: str
    english: str
    # "repair": the matcher guessed this word from a likely recognition
    # mistake ("pearl" -> "pear"); the page shows it as a guess.
    source: Literal["lexicon", "repair", "english", "translation"]


class Generated(TypedDict):
    status: Status
    prompt: str | None
    frames: list[str]
    episodes: int
    slots: list[Slot]
    untranslated: list[str]
    dataset_original: str | None
    repairs: list[dict[str, str]]  # {"heard", "as"}: every guessed word


# ------------------------------------------------------------------ frames ----
def instruction_text(raw: str) -> str:
    """A dataset instruction as a frame source: no ``Reset:``, lower case."""
    return re.sub(r"\s+", " ", _RESET.sub("", raw)).strip().lower()


def _action_of(verb: str) -> str | None:
    concept = lexicon.EN_CONCEPTS.get(verb)
    return concept if concept and concept.startswith("A:") else None


def _destination(step: str) -> tuple[str | None, str, str] | None:
    """(action or None, preposition, destination noun concept) of a dataset
    step "[verb] A place B"; a step without its verb ("... then peach in red
    basket") still tells which preposition goes with the destination."""
    words = _WORD.findall(step)
    if _NOT_SIMPLE.intersection(words):
        return None
    places = [i for i, w in enumerate(words) if w in _PLACES and i > 0]
    if len(places) != 1:
        return None
    for word in reversed(words[places[0] + 1 :]):
        found = (
            lexicon.EN_CONCEPTS.get(word)
            or ((matcher._lookup_english(word) or (None,))[0])
        )
        if found and found.startswith("N:"):
            return _action_of(words[0]), words[places[0]], found
    return None


def _frame_of_step(step: str) -> tuple[str, str, str, bool, bool] | None:
    words = _WORD.findall(step)
    if len(words) < 4 or _NOT_SIMPLE.intersection(words):
        return None
    action = _action_of(words[0])
    if not action:
        return None
    places = [i for i, w in enumerate(words) if w in _PLACES and i > 1]
    if len(places) != 1:
        return None
    at = places[0]
    a, b = words[1:at], words[at + 1 :]
    the_a = bool(a) and a[0] in _ARTICLES
    the_b = bool(b) and b[0] in _ARTICLES
    a = a[1:] if the_a else a
    b = b[1:] if the_b else b
    if not a or not b or _ARTICLES.intersection(a + b):
        return None
    return action, words[0], words[at], the_a, the_b


def learn_frames(counts: Mapping[str, int]) -> Frames:
    """The most common frame per action, weighted by episodes.

    ``counts`` maps a dataset instruction (as written) to its episodes. Only
    steps of the form "verb [the] A place [the] B" count; the second step of
    "X then Y" is used when it repeats its verb."""
    tally: dict[str, Counter[tuple[str, str, bool, bool]]] = defaultdict(Counter)
    examples: dict[tuple[str, str, str, bool, bool], Counter[str]] = defaultdict(
        Counter
    )
    sentences: Counter[str] = Counter()
    places: dict[str, Counter[str]] = defaultdict(Counter)
    for raw, episodes in counts.items():
        if not isinstance(raw, str) or not isinstance(episodes, int) or episodes <= 0:
            continue
        text = instruction_text(raw)
        if not text:
            continue
        if _offerable(raw):
            sentences[raw.strip()] += episodes
        previous: str | None = None
        for step in _STEP_SPLIT.split(text):
            found = _frame_of_step(step)
            if found:
                action, *rest = found
                key = tuple(rest)
                tally[action][key] += episodes  # type: ignore[index]
                examples[found][text] += episodes
            destination = _destination(step)
            if destination:
                # A step without its verb continues the previous step's action.
                verb_action, place, noun = destination
                verb_action = verb_action or previous
                places[noun][place] += episodes
                if verb_action:
                    places[f"{verb_action} {noun}"][place] += episodes
            words = _WORD.findall(step)
            previous = (_action_of(words[0]) if words else None) or previous
    by_action: dict[str, Frame] = {}
    for action, counter in tally.items():
        # The verb is chosen on its own (the preposition follows the
        # destination, so "pick A on B" and "pick A into B" both vote for
        # "pick"); then that verb's most common wording is the fallback.
        # Ties go to the alphabetically first so dictionary order never decides.
        verbs: Counter[str] = Counter()
        for key, n in counter.items():
            verbs[key[0]] += n
        verb = min(verbs.items(), key=lambda item: (-item[1], item[0]))[0]
        (_, place, the_a, the_b), _n = min(
            ((k, n) for k, n in counter.items() if k[0] == verb),
            key=lambda item: (-item[1], item[0]),
        )
        episodes = verbs[verb]
        example = min(
            examples[(action, verb, place, the_a, the_b)].items(),
            key=lambda item: (-item[1], item[0]),
        )[0]
        by_action[action] = Frame(action, verb, place, the_a, the_b, episodes, example)
    place_for = {
        noun: min(counter.items(), key=lambda item: (-item[1], item[0]))[0]
        for noun, counter in places.items()
    }
    return Frames(by_action, place_for, dict(sentences))


def _offerable(raw: str) -> bool:
    """A dataset instruction that may be offered as written: a task (not a
    "Reset:" one), and read by the strict matcher without a flag or an
    unknown word."""
    if _RESET.match(raw) or raw != raw.strip() or not raw:
        return False
    text, parsed = matcher._candidate(raw)
    return matcher._flag(text, parsed) is None and parsed.unknown_units == 0


# ---------------------------------------------------------------- reading ----
_ENGLISH_FORM: Final[dict[str, str]] = {}
for _form, _concept in lexicon.EN_CONCEPTS.items():
    _ENGLISH_FORM.setdefault(_concept, _form)  # the first spelling listed


@dataclass(frozen=True)
class _Thing:
    modifiers: frozenset[str]
    noun: str | None  # lexicon concept, or None for a word outside the lexicon
    said: str  # what was heard for the noun
    repaired: bool = False  # the noun is the matcher's guess
    placed: bool = False  # a place word ("into", "on") or a verb came before it


@dataclass(frozen=True)
class _Step:
    acts: tuple[str, ...]
    things: tuple[_Thing, ...]
    relations: frozenset[str]


class _SlotReader(matcher._Reader):
    """The matcher's reader, also keeping where an unknown word was heard and
    what it could not keep at all (a number, another script)."""

    def __init__(self, repair: bool) -> None:
        super().__init__(repair)
        self.dropped: list[str] = []

    def miss(self, text: str, units: int = 1) -> None:
        super().miss(text, units)
        if matcher._ENGLISH_WORD.fullmatch(text) or matcher._CJK.fullmatch(text):
            self.tokens.append(matcher._Tok("U", "unknown", text, text))
        else:
            self.dropped.append(text)


def _read(words: str) -> tuple[list[matcher._Tok], set[str], list[str]]:
    """Tokens (veto words left out; "P" marks a place word), the concepts
    heard, and the words no token holds."""
    reader = _SlotReader(repair=True)
    pieces = matcher._TOKEN.findall(words)
    at = 0
    while at < len(pieces):
        piece = pieces[at]
        if piece in _PLACES:
            reader.tokens.append(matcher._Tok("P", "exact", piece, piece))
            at += 1
        elif matcher._CJK.fullmatch(piece):
            reader.chinese(piece)
            at += 1
        elif piece[0].isdigit():
            reader.miss(piece)
            at += 1
        else:
            at = reader.english(pieces, at)
    concepts = {t.concept for t in reader.tokens}
    kept = [t for t in reader.tokens if t.concept not in matcher._VETO]
    return kept, concepts, reader.dropped


def _steps(tokens: Sequence[matcher._Tok]) -> list[_Step]:
    groups: list[list[matcher._Tok]] = [[]]
    for tok in tokens:
        if tok.concept == "T":
            if groups[-1]:
                groups.append([])
        else:
            groups[-1].append(tok)
    steps = []
    for group in groups:
        if not group:
            continue
        acts: list[str] = []
        relations: set[str] = set()
        things: list[_Thing] = []
        pending: set[str] = set()
        placed = False
        last = ""
        for tok in group:
            kind = tok.concept[0]
            if tok.concept == "U" and last == "U" and not pending:
                # Unknown words next to each other name one thing: "dragon fruit".
                joined = things[-1]
                things[-1] = _Thing(
                    joined.modifiers,
                    None,
                    f"{joined.said} {tok.heard}",
                    placed=joined.placed,
                )
            elif tok.concept == "U":
                things.append(
                    _Thing(frozenset(pending), None, tok.heard, placed=placed)
                )
                pending, placed = set(), False
            elif tok.concept == "P":
                placed = True
            elif kind == "A":
                acts.append(tok.concept)
                # A Chinese verb carries its place: 放到, 放进, 叠在.
                placed = placed or bool(matcher._CJK.fullmatch(tok.heard))
            elif kind == "R":
                relations.add(tok.concept)
            elif kind == "J":
                pending.add(tok.concept)
            elif kind == "N":
                things.append(
                    _Thing(
                        frozenset(pending),
                        tok.concept,
                        tok.heard,
                        repaired=tok.repaired,
                        placed=placed,
                    )
                )
                pending, placed = set(), False
            if tok.concept != "P":
                last = tok.concept
        if pending and things:
            end = things[-1]
            things[-1] = _Thing(
                end.modifiers | frozenset(pending),
                end.noun,
                end.said,
                end.repaired,
                end.placed,
            )
        steps.append(_Step(tuple(acts), tuple(things), frozenset(relations)))
    return steps


def _phrase(thing: _Thing, noun: str) -> str:
    order = [m for m in _SIZE_FIRST if m in thing.modifiers]
    order += sorted(m for m in thing.modifiers if m not in _SIZE_FIRST)
    return " ".join([_ENGLISH_FORM[m] for m in order] + [noun])


def _empty(status: Status, untranslated: Sequence[str] = ()) -> Generated:
    return {
        "status": status,
        "prompt": None,
        "frames": [],
        "episodes": 0,
        "slots": [],
        "untranslated": list(untranslated),
        "dataset_original": None,
        "repairs": [],
    }


# Words that say where or how, never what: not an object name.
_NOT_NAMES: Final = frozenset(
    """left right up down back out forward forwards backward backwards away
    here there over under front behind side top bottom middle center centre
    now again later quick slow gently carefully please thing stuff""".split()  # noqa: SIM905
)


def _said_name(said: str) -> bool:
    """An object name said in English: one to three words, none of them a
    direction or a manner (the words that ended up as "pick apple on left")."""
    words = said.split(" ")
    return len(words) <= 3 and not any(
        w in _NOT_NAMES or w.endswith("ly") for w in words
    )


def unknown_names(transcript: str) -> list[str]:
    """The Chinese object names outside the lexicon (what a translator gets)."""
    if not isinstance(transcript, str):
        return []
    tokens, _, _ = _read(matcher._clean(transcript).words)
    return sorted(
        {
            t.heard
            for t in tokens
            if t.concept == "U" and matcher._CJK.fullmatch(t.heard)
        }
    )


def generate(
    transcript: str,
    frames: Frames,
    *,
    family: str,
    translate: Callable[[str], str | None] | None = None,
) -> Generated:
    """One sentence in the datasets' most common phrasing, or why there is none.

    ``translate`` turns a Chinese object name outside the lexicon into English
    (``None`` when it cannot); without it such a name leaves the sentence out
    with status ``untranslated``. Never raises on any string."""
    if not isinstance(transcript, str):
        return _empty("no_task")
    if family != "all":
        return _empty("restricted")
    text = matcher._clean(transcript)
    tokens, concepts, dropped = _read(text.words)
    if text.unsafe or {"STOP", "NEG"} & concepts:
        return _empty("vetoed")
    if dropped:
        return _empty("unclear")
    steps = _steps(tokens)
    if not steps:
        return _empty("no_task")
    rendered: list[str] = []
    used: list[Frame] = []
    actions: list[str] = []
    slots: list[Slot] = []
    untranslated: list[str] = []
    action: str | None = None
    for step in steps:
        if step.relations:
            return _empty("unsupported")
        # "pick up the cup and pour it": the specific action is the task; two
        # specific ones in one step are more than a frame can say.
        specific = sorted({a for a in step.acts if a != "A:MOVE"})
        if len(specific) > 1:
            return _empty("unsupported")
        action = specific[0] if specific else (step.acts[0] if step.acts else action)
        if action is None or len(step.things) != 2:
            return _empty("no_task")
        destination = step.things[1]
        if destination.noun is None and not destination.placed:
            return _empty("no_task")  # "move the apple left": no place was said
        frame = frames.by_action.get(action)
        if frame is None:
            return _empty("no_frame")
        actions.append(action)
        nouns = []
        for thing in step.things:
            if thing.noun is not None:
                english = _ENGLISH_FORM[thing.noun]
                source = "repair" if thing.repaired else "lexicon"
            elif matcher._CJK.fullmatch(thing.said):
                english = translate(thing.said) if translate else None
                english = _english_name(english)
                source = "translation"
                if english is None:
                    untranslated.append(thing.said)
                    continue
            elif _said_name(thing.said):
                english, source = thing.said, "english"
            else:
                return _empty("unclear")
            slots.append({"said": thing.said, "english": english, "source": source})
            nouns.append(_phrase(thing, english))
        if len(nouns) != 2:
            continue
        a = f"the {nouns[0]}" if frame.the_a else nouns[0]
        b = f"the {nouns[1]}" if frame.the_b else nouns[1]
        place = _place(frames, frame, step.things[1].noun)
        rendered.append(f"{frame.verb} {a} {place} {b}")
        used.append(Frame(**{**frame.__dict__, "place": place}))
    if untranslated:
        return _empty("untranslated", sorted(set(untranslated)))
    prompt = " then ".join(rendered)
    if not _reads_back(prompt, steps, actions):
        return _empty("no_task")
    original = _dataset_original(prompt, frames)
    return {
        "status": "ok",
        "prompt": prompt,
        "frames": [f.pattern() for f in used],
        "episodes": min(f.episodes for f in used),
        "slots": slots,
        "untranslated": [],
        "dataset_original": original,
        "repairs": [{"heard": t.heard, "as": t.spelled} for t in tokens if t.repaired],
    }


def _place(frames: Frames, frame: Frame, destination: str | None) -> str:
    """The preposition before B: what the datasets use for this action and
    destination; for a move, what they use for the destination with any
    action ("into" a bowl, "on" a plate); else the action's own."""
    if destination:
        found = frames.place_for.get(f"{frame.action} {destination}")
        if found:
            return found
        if frame.action == "A:MOVE" and destination in frames.place_for:
            return frames.place_for[destination]
    return frame.place


# A translated name: one to three words of letters (a hyphen inside a word),
# with at most a final full stop, which translators add.
_NAME: Final = re.compile(r"[a-z]+(?:-[a-z]+)*(?: [a-z]+(?:-[a-z]+)*){0,2}")


def _english_name(value: str | None) -> str | None:
    """A translated object name, or None when it is not one to three plain
    English words (a sentence or a stray mark is never put in a slot)."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value.strip().lower()).removesuffix(".")
    if len(text.split(" ")) > 3:
        return None
    words = [w for w in text.split(" ") if w not in _ARTICLES]
    name = " ".join(words)
    if not _NAME.fullmatch(name):
        return None
    concepts = {
        matcher._lookup_english(w)[0] for w in words if matcher._lookup_english(w)
    }
    if concepts & matcher._VETO or any(
        c.startswith(("A:", "T", "R:")) for c in concepts
    ):
        return None
    return name


def _reads_back(prompt: str, steps: Sequence[_Step], actions: Sequence[str]) -> bool:
    """The reader must find the heard steps in the sentence: the same specific
    action (a move is the generic one), the same objects with the same
    modifiers, in order, and nothing it could not read."""
    text = matcher._clean(prompt)
    if not prompt or text.unsafe:
        return False
    tokens, concepts, dropped = _read(text.words)
    if {"STOP", "NEG"} & concepts or dropped:
        return False
    again = _steps(tokens)
    if len(again) != len(steps) or len(actions) != len(steps):
        return False
    for heard, written, action in zip(steps, again, actions, strict=True):
        if set(written.acts) - {"A:MOVE"} != {action} - {"A:MOVE"}:
            return False
        if len(heard.things) != len(written.things):
            return False
        for h, w in zip(heard.things, written.things, strict=True):
            if h.modifiers != w.modifiers or (h.noun is not None and h.noun != w.noun):
                return False
    return True


def _dataset_original(prompt: str, frames: Frames) -> str | None:
    """A dataset instruction that means the same steps, if there is one (the
    policy may know it better than the generated wording)."""
    want = matcher._signature(prompt)
    if any(not clause.things for clause in want):
        return None
    found = [
        (episodes, sentence)
        for sentence, episodes in frames.sentences.items()
        if sentence.lower() != prompt and matcher._signature(sentence) == want
    ]
    if not found:
        return None
    return min(found, key=lambda item: (-item[0], item[1]))[1]


def counts_from(rows: Iterable[tuple[str, int]]) -> dict[str, int]:
    """Sum episodes per instruction (several datasets may share one)."""
    total: Counter[str] = Counter()
    for text, episodes in rows:
        if isinstance(text, str) and isinstance(episodes, int) and episodes > 0:
            total[text] += episodes
    return dict(total)
