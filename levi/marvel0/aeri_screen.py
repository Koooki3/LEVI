"""What terminal D's screen says (pure; no file, process or clock).

Terminal D (``aeri.py execute``, the page-hosted AERI run) is a program that
prints lines and waits for keys. The page decides what to offer from three
sources: D's state journal, its heartbeat file and **this screen**
(``aeri_state.derive``). This module turns the bytes D wrote into lines and
tells D's own lines apart:

* ``ScreenBuf`` is a small terminal: it removes colour and cursor sequences,
  lets a lone carriage return go back to the start of the line, keeps the
  half-written last line apart (``Type execute <id>: `` has no newline) and
  numbers every line with the byte offset it started at (``prompt.id``: a
  prompt that is printed again gets a new id, so a key meant for the first
  one is never given to the second).
* ``Lexicon`` knows the words of one AERI code version and one language
  (``aeri_prompts.json``, copied from ``console_text.py`` by
  ``tests/marvel0/regen_aeri_prompts.py``). Each template ``{x}`` becomes a
  group; every line is matched in full, never by a fragment, so a changed
  sentence is **unrecognised** (and the page turns careful) instead of
  matching something else.
* ``read(buf, lex)`` is the view the state rules read: which prompt is open
  (the last line that is D's own and was not answered), the object rows, the
  wait block, the forward progress, whether a Ctrl+C was acknowledged.

**An unrecognised line never closes a prompt.** D's output also holds lines of
other programs (``Homing over 30 steps``, ``server config:``, the ``uv``
warnings, ``Gripper reply confirmed``): they are matched by the ``foreign``
patterns and ignored, and any other line that is not D's is ignored the same
way, counted, and reported so that a sentence that changed shows up as drift.
What does close a prompt is a line that **is** D's (a template, or anything
that starts with the round tag ``[第 n/N 轮]`` / ``[准备]`` that ``Console.line``
prefixes).
"""

import codecs
import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

PROMPTS_FILE = Path(__file__).with_name("aeri_prompts.json")
LINES_KEPT = 1500  # committed lines held in memory
LINE_MAX = 600  # characters of a line kept
TAIL_LINES = 60  # lines of the document's screen tail
CARRY_MAX = 64  # an escape sequence cut by a read is carried to the next one
MIN_LITERAL = 8  # a template with less fixed text is a fragment, not a line

_CSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ESC2 = re.compile(r"\x1b[()*+][0-9A-Za-z]|\x1b[@-Z\\-_=>78]")
# An escape sequence that the end of the read cut in two.
_CUT = re.compile(r"\x1b(?:\[[0-9;?<>=]*[ -/]*|\][^\x07\x1b]*\x1b?|[()*+]?)$")

PLACEHOLDER = re.compile(r"(\{[a-z_]+\})")

# How D's templates become kinds the rules know. ``key -> kind``; every other
# key of a prompt file that matches a line is the kind ``text`` (D's, harmless).
KINDS = {
    "run_confirm_warning": "warning",
    "swap_unclean_head": "swap_head",
    "run_cancelled": "cancelled",
    "startup_home_object_handled": "home_o",
    "startup_home_path_clear": "home_p",
    "startup_home_gripper_will_open": "home_h",
    "sub_release": "release",
    "sub_released": "released",
    "round_start": "round_start",
    "progress_subtask": "progress",
    "subtask_end_shown": "subtask_end",
    "sub_inv_row": "inv_row",
    "recent_note": "recent_note",
    "interrupt": "interrupt",
    "confirm_cancelled": "confirm_cancelled",
    "svc_shutdown": "svc_shutdown",
}
# Keys of a tagged line's body (``Console.line``) and their kinds.
BODY_KINDS = {
    "sub_inv_title": "inv_title",
    "sub_inv_todo": "inv_todo",
    "sub_inv_yes": "inv_yes",
    "sub_inv_no": "inv_no",
    "sub_forward_hint": "forward_hint",
    "sub_reverse_hint": "reverse_hint",
    "sub_reverse_requested": "reverse_requested",
    "sub_reverse_ignored": "reverse_ignored",
    "verdict_taken": "verdict_taken",
    "verdict_refused": "verdict_refused",
    "confirm_resume": "confirm_resume",
    "st_resumed": "resumed",
    "st_resumed_by_page": "resumed",
    "st_completed": "completed",
    "st_stopped": "stopped",
}
STATUS_PREFIXES = ("st_",)


def template_regex(template: str) -> tuple:
    """``(compiled full-line regex, placeholder names)``. Trailing white space
    is not compared (the English phrase prompt ends with a blank)."""
    parts = PLACEHOLDER.split(template.rstrip())
    names, pattern = [], []
    for part in parts:
        if PLACEHOLDER.fullmatch(part):
            names.append(part[1:-1])
            pattern.append("(.+?)")
        else:
            pattern.append(re.escape(part))
    return re.compile("^" + "".join(pattern) + "$"), names


@dataclass(frozen=True)
class Line:
    text: str
    start: int  # byte offset of the line's first byte
    end: int  # byte offset after its newline (the end of the buffer if partial)


class ScreenBuf:
    """D's output as lines. ``feed`` takes the bytes in order; the buffer
    started at ``base`` bytes into the file."""

    def __init__(self, base: int = 0):
        self.base = base
        self.offset = base  # bytes fed so far, in file coordinates
        self.lines: deque = deque(maxlen=LINES_KEPT)
        self.count = 0  # committed lines ever (a line's number is count at its commit)
        self.bytes_seen = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._carry = ""
        self._cur: list = []
        self._pos = 0
        self._start = base  # offset of the current line's first byte
        # What ``read`` derived from this buffer, so that a look at a screen that
        # did not change costs nothing and a new line is classified once: the
        # ``Seen`` of every committed line (aligned with ``lines``), the lexicon
        # and line count they were made with, and the last view with its key.
        self._seen: deque = deque(maxlen=LINES_KEPT)
        self._seen_lex = None
        self._seen_count = 0
        self._view_key: tuple | None = None
        self._view = None

    # ---- input

    def feed(self, data: bytes) -> None:
        if not data:
            return
        self.bytes_seen += len(data)
        text = self._carry + self._decoder.decode(data)
        self._carry = ""
        match = _CUT.search(text)
        if match and len(text) - match.start() <= CARRY_MAX:
            self._carry, text = text[match.start() :], text[: match.start()]
        text = _OSC.sub("", _CSI.sub("", text))
        text = _ESC2.sub("", text)
        for ch in text:
            self._char(ch)

    def _char(self, ch: str) -> None:
        width = len(ch.encode("utf-8", "replace"))
        if ch == "\n":
            self.offset += width
            self._commit()
        elif ch == "\r":
            self.offset += width
            self._pos = 0
        elif ch == "\b":
            self.offset += width
            self._pos = max(0, self._pos - 1)
        else:
            self.offset += width
            if ch == "\t":
                ch = " "
            elif ord(ch) < 0x20 or ch == "\x7f":
                return
            if self._pos < len(self._cur):
                self._cur[self._pos] = ch
            else:
                self._cur.append(ch)
            self._pos += 1

    def answered(self) -> None:
        """A line was answered without an echo (the phrase: the terminal's echo
        is off, so no newline follows what D left unfinished): end it here, so
        that D's next line does not stick to it."""
        if self._cur:
            self._commit()

    def _commit(self) -> None:
        text = "".join(self._cur)[:LINE_MAX]
        self.lines.append(Line(text, self._start, self.offset))
        self.count += 1
        self._cur, self._pos, self._start = [], 0, self.offset

    # ---- output

    @property
    def partial(self) -> Line | None:
        """The unfinished last line (a prompt that waits without a newline)."""
        if not self._cur:
            return None
        return Line("".join(self._cur)[:LINE_MAX], self._start, self.offset)

    def tail(self, n: int = TAIL_LINES) -> list:
        """The last ``n`` lines of text, the unfinished one included."""
        lines = [line.text for line in list(self.lines)[-n:]]
        partial = self.partial
        if partial is not None:
            lines = (lines + [partial.text])[-n:]
        return lines


@dataclass(frozen=True)
class Seen:
    """One classified line."""

    kind: str  # a kind of KINDS/BODY_KINDS, "svc", "wait", "wait_phase", "phrase", ...
    key: str | None  # the template key it matched
    text: str
    start: int
    end: int
    index: int  # the line's number (ScreenBuf.count at its commit; -1 if partial)
    n: int | None = None  # the round number of a tag or of a "Round n/N" line
    total: int | None = None
    groups: tuple = ()
    names: tuple = ()
    tagged: bool = False

    def group(self, name, default=None):
        try:
            return self.groups[self.names.index(name)]
        except (ValueError, IndexError):
            return default


class Lexicon:
    """The words of one profile (``aeri_prompts.json``) in one language."""

    def __init__(self, profile: dict, foreign: list, lang: str = "zh"):
        if lang not in profile["texts"]:
            raise ValueError(f"no words in {lang}")
        self.lang = lang
        self.profile = profile
        texts = profile["texts"][lang]
        self.texts = texts
        self.foreign = [re.compile(p) for p in foreign]
        tag = "".join(
            r"(\d+)" if part in ("{n}", "{total}") else re.escape(part)
            for part in PLACEHOLDER.split(texts["tag"])
        )
        self.tag_re = re.compile(
            "^(?:" + tag + "|" + re.escape(texts["tag_setup"]) + ") (.*)$"
        )
        self.tag_setup = texts["tag_setup"]
        self.join = texts["join"]
        self.plain: list = []  # (key, kind, regex, names) of untagged templates
        heads: dict = {}  # key -> the template's fixed text before its first {x}
        for key, template in texts.items():
            if key in ("tag", "tag_setup", "join") or key in BODY_KINDS:
                continue
            if key.startswith(("wait_", "todo_")):
                continue
            if (
                key not in KINDS
                and not key.startswith(("svc_", "sum_"))
                and len(PLACEHOLDER.sub("", template).strip()) < MIN_LITERAL
            ):
                continue  # a fragment of a longer sentence ("成功", "步数上限"), never a line
            if key.startswith("svc_") and key not in ("svc_pass", "svc_fail"):
                # a step line ends in the pass/fail word (Services.step)
                template = template.rstrip() + "{suffix}"
                regex, names = template_regex(template)
                regex = re.compile(
                    regex.pattern[: -len("(.+?)$")]
                    + "(?: (?:"
                    + re.escape(texts["svc_pass"])
                    + "|"
                    + re.escape(texts["svc_fail"])
                    + "))?$"
                )
                names = names[:-1]
            else:
                regex, names = template_regex(template)
            self.plain.append((key, self._kind_of(key), regex, tuple(names)))
            heads[key] = PLACEHOLDER.split(template.rstrip())[0]
        # Most literal first: a template with fewer placeholders is more specific.
        self.plain.sort(key=lambda item: (len(item[3]), -len(item[2].pattern)))
        # Each regex is anchored at its fixed head, so a line can only match the
        # templates whose head starts with the line's first character, or that have
        # none: those, in the same order, are all ``classify`` tries.
        self._plain_any = [item for item in self.plain if not heads[item[0]]]
        self._plain_by_first = {
            first: [
                item
                for item in self.plain
                if not heads[item[0]] or heads[item[0]][0] == first
            ]
            for first in {head[0] for head in heads.values() if head}
        }
        self.body: list = []  # tagged bodies
        for key, kind in BODY_KINDS.items():
            if key in texts:
                regex, names = template_regex(texts[key])
                self.body.append((key, kind, regex, tuple(names)))
        for key in texts:
            if key.startswith("st_") and key not in BODY_KINDS:
                regex, names = template_regex(texts[key])
                self.body.append((key, "status", regex, tuple(names)))
        for key in texts:
            if key.startswith("sub_wait_"):
                regex, names = template_regex(texts[key])
                self.body.append((key, "wait_phase", regex, tuple(names)))
        # wait blocks: happened + join + todo, for every pair the profile can print
        self.waits: list = []
        pairs = self._wait_pairs()
        for happened_key, todo_key, reason in pairs:
            happened = texts.get(happened_key)
            todo = texts.get(todo_key)
            if happened is None or todo is None:
                continue
            body = self.join.replace("{happened}", happened).replace("{todo}", todo)
            regex, names = template_regex(body)
            self.waits.append((happened_key, todo_key, reason, regex, tuple(names)))
        self._phrase_re = template_regex(texts["run_type_phrase"])[0]
        self.grasp_word = texts["sub_inv_grasp"]
        self.restore_todos = set(profile.get("restore_todos", ()))
        self.restore_phases = set(profile.get("restore_phases", ()))

    @staticmethod
    def _kind_of(key: str) -> str:
        if key in KINDS:
            return KINDS[key]
        if key.startswith("svc_"):
            return "svc"
        if key.startswith(("fault_", "advice_")):
            return "fault"
        if key == "run_type_phrase":
            return "phrase_line"  # a finished line of it is not a prompt that waits
        return "text"

    def _wait_pairs(self) -> list:
        """``(happened key, todo key, reason)`` for every wait the console can
        print, from the profile's tables (``WAIT_REASONS`` and the notes that
        pick a more specific sentence) plus the generic one."""
        pairs = []
        reasons = self.profile["wait_reasons"]
        for reason, (happened, todo) in reasons.items():
            pairs.append((happened, todo, reason))
            for happened_key in self.profile["wait_notes"].get(reason, {}).values():
                pairs.append((happened_key, todo, reason))
        pairs.append(("wait_other", "todo_other_resume", "other"))
        return pairs

    # ---- classification

    def is_noise(self, text: str) -> bool:
        return any(p.search(text) for p in self.foreign)

    def classify(self, line: Line, index: int = -1) -> Seen:
        text = line.text
        base = {"text": text, "start": line.start, "end": line.end, "index": index}
        tagged = self.tag_re.match(text)
        if tagged:
            groups = tagged.groups()
            n, total, body = (
                (int(groups[0]), int(groups[1]), groups[2])
                if groups[0] is not None
                else (None, None, groups[2])
            )
            found = self._body(body, base, n, total)
            if found is not None:
                return found
            return Seen("tagged", None, n=n, total=total, tagged=True, **base)
        stripped = text.rstrip()
        candidates = self._plain_by_first.get(stripped[:1], self._plain_any)
        for key, kind, regex, names in candidates:
            match = regex.match(stripped)
            if match:
                groups = match.groups()
                seen = Seen(kind, key, groups=groups, names=names, **base)
                return self._plain_extras(seen)
        if not text.strip():
            return Seen("blank", None, **base)
        if self.is_noise(text):
            return Seen("noise", None, **base)
        return Seen("unrecognised", None, **base)

    def _plain_extras(self, seen: Seen) -> Seen:
        """The round of a line that names it (``Round n/N`` ...)."""
        n, total = seen.group("n"), seen.group("total")
        if seen.kind in ("release", "released", "round_start") and n and total:
            try:
                return Seen(**{**seen.__dict__, "n": int(n), "total": int(total)})
            except ValueError:
                return seen
        return seen

    def _body(self, body: str, base: dict, n, total):
        for happened_key, todo_key, reason, regex, names in self.waits:
            match = regex.match(body)
            if match:
                return Seen(
                    "wait",
                    happened_key,
                    n=n,
                    total=total,
                    tagged=True,
                    groups=(todo_key, reason, *match.groups()),
                    names=("todo_key", "reason", *names),
                    **base,
                )
        for key, kind, regex, names in self.body:
            match = regex.match(body)
            if match:
                return Seen(
                    kind,
                    key,
                    n=n,
                    total=total,
                    tagged=True,
                    groups=match.groups(),
                    names=names,
                    **base,
                )
        return None

    def phrase_of(self, text: str) -> str | None:
        """The phrase a ``Type <phrase>:`` prompt asks for, or None."""
        match = self._phrase_re.match(text.rstrip())
        return match.group(1) if match else None


@dataclass
class ScreenView:
    """What the rules read of the screen."""

    lines: int = 0  # committed lines so far
    bytes: int = 0
    seen: list = field(default_factory=list)  # classified lines, newest last
    open: Seen | None = None  # the prompt that waits, if D's last line is one
    block: list = field(default_factory=list)  # lines of that prompt's block
    unrecognised: int = 0  # lines that are no one's (drift candidates), ever
    unrecognised_after_open: int = 0
    last_d_index: int = -1  # the number of the last D line
    counts: dict = field(default_factory=dict)  # kind -> how many
    last_index: dict = field(default_factory=dict)  # kind -> number of its last line
    steps: tuple | None = None  # (step, max) of the last forward progress line
    interrupt: bool = False  # D acknowledged a Ctrl+C
    completed: tuple | None = None  # ("completed"|"stopped", Seen)
    fault: str | None = None  # the code of a fault block
    inventory_rows: list = field(default_factory=list)  # parsed rows of the open list
    inventory_rows_last: list = field(default_factory=list)  # rows of the latest list
    wait: dict | None = None  # the wait block of the open prompt
    round: int | None = None  # the round of the last tagged line


# Kinds that make a prompt "answered"/superseded when they come after it.
OPEN_KINDS = (
    "phrase",
    "inv_todo",
    "home_o",
    "home_p",
    "home_h",
    "release",
    "confirm_resume",
    "wait",
    "forward_hint",
)
TRAILERS = (
    "wait_phase",
    "recent_note",
    "noise",
    "blank",
    "unrecognised",
    "progress",
)


def read(buf: ScreenBuf, lex: Lexicon) -> ScreenView:
    """The classified screen and its open prompt.

    The watcher looks four times a second and the 1500 lines a full screen
    holds used to be classified at every look (about 15 ms of CPU each). Now
    every committed line is classified **once** (``_classified``) and a look at a
    buffer that did not change since the last one with the same lexicon returns
    **the same view object**: the rules only read a view, never change one."""
    partial = buf.partial
    key = (buf.count, buf.bytes_seen, buf.offset, len(buf.lines), partial)
    if buf._view is not None and buf._seen_lex is lex and buf._view_key == key:
        return buf._view
    view = _read(buf, lex, partial)
    buf._view_key, buf._view = key, view
    return view


def _classified(buf: ScreenBuf, lex: Lexicon) -> list:
    """The ``Seen`` of every committed line of ``buf``, oldest first. Lines are
    only ever appended (the oldest drop off the front), so only the ones
    committed since the last call are classified; anything that does not line up
    (another lexicon, a buffer changed some other way) classifies them all."""
    lines = buf.lines
    cache = buf._seen
    first = buf.count - len(lines)
    new = buf.count - buf._seen_count
    if buf._seen_lex is lex and 0 <= new <= len(lines):
        for pos in range(len(lines) - new, len(lines)):
            cache.append(lex.classify(lines[pos], first + pos + 1))
        if len(cache) != len(lines) or (
            lines
            and (
                cache[0].start != lines[0].start
                or cache[-1].start != lines[-1].start
                or cache[-1].end != lines[-1].end
            )
        ):
            new = -1  # (out of step: start again)
    else:
        new = -1
    if new < 0:
        cache.clear()
        cache.extend(lex.classify(line, first + i + 1) for i, line in enumerate(lines))
    buf._seen_lex, buf._seen_count = lex, buf.count
    return list(cache)


def _read(buf: ScreenBuf, lex: Lexicon, partial: Line | None) -> ScreenView:
    view = ScreenView(lines=buf.count, bytes=buf.bytes_seen)
    seen = _classified(buf, lex)
    if partial is not None:
        phrase = lex.phrase_of(partial.text)
        if phrase is not None:
            kind = (
                "phrase_variant"
                if phrase.endswith(" after policy switch check")
                else "phrase"
            )
            seen.append(
                Seen(
                    kind,
                    "run_type_phrase",
                    partial.text,
                    partial.start,
                    partial.end,
                    -1,
                    groups=(phrase,),
                    names=("phrase",),
                )
            )
        elif lex.is_noise(partial.text):
            seen.append(
                Seen("noise", None, partial.text, partial.start, partial.end, -1)
            )
        else:
            seen.append(
                Seen("partial", None, partial.text, partial.start, partial.end, -1)
            )
    view.seen = seen
    for item in seen:
        view.counts[item.kind] = view.counts.get(item.kind, 0) + 1
        if (
            item.kind not in ("noise", "blank", "unrecognised", "partial")
            or item.tagged
        ):
            view.last_d_index = max(view.last_d_index, item.index)
        view.last_index[item.kind] = max(view.last_index.get(item.kind, -1), item.index)
        if item.kind == "unrecognised":
            view.unrecognised += 1
        if item.n is not None and item.tagged:
            view.round = item.n
        if item.kind == "progress":
            step, top = item.group("steps"), item.group("max_steps")
            if step and top and step.isdigit() and top.isdigit():
                view.steps = (int(step), int(top))
        elif item.kind == "interrupt":
            view.interrupt = True
        elif item.kind in ("completed", "stopped"):
            view.completed = (item.kind, item)
        elif item.kind == "fault" and item.key == "fault_title":
            view.fault = item.group("code")
    # the open prompt: the last line that is D's, once the trailers are skipped
    for i in range(len(seen) - 1, -1, -1):
        item = seen[i]
        if item.kind in TRAILERS or item.kind == "partial":
            if item.kind in ("unrecognised", "partial"):
                view.unrecognised_after_open += 1
            continue
        if item.kind in OPEN_KINDS or item.kind == "phrase_variant":
            view.open = item
            view.block = seen[i:]
        break
    todos = [s for s in seen if s.kind == "inv_todo"]
    if todos:
        view.inventory_rows_last = _inventory_rows(seen, todos[-1], lex)
    if view.open is not None and view.open.kind == "inv_todo":
        view.inventory_rows = view.inventory_rows_last
    if view.open is not None and view.open.kind in ("wait", "confirm_resume"):
        view.wait = _wait_block(seen, view.open, lex)
    return view


def _inventory_rows(seen: list, todo: Seen, lex: Lexicon) -> list:
    """The rows of the object list that ends in ``todo``: those between the
    nearest title before it and ``todo``, as printed."""
    at = next(i for i, s in enumerate(seen) if s is todo)
    rows = []
    for item in reversed(seen[:at]):
        if item.kind == "inv_title":
            break
        if item.kind == "inv_row":
            rows.append(
                {
                    "id": item.group("id"),
                    "colour": item.group("colour"),
                    "kind": item.group("kind"),
                    "on": item.group("on"),
                    "graspable": item.group("grasp") == lex.grasp_word,
                }
            )
    rows.reverse()
    return rows


def _wait_block(seen: list, open_: Seen, lex: Lexicon) -> dict:
    """The wait the prompt belongs to: the reason, its sentence keys, the phase
    line and the notes D prints under it. A ``confirm_resume`` prompt belongs to
    the wait line before it."""
    at = next(i for i, s in enumerate(seen) if s is open_)
    head = open_ if open_.kind == "wait" else None
    if head is None:
        for item in reversed(seen[:at]):
            if item.kind == "wait":
                head = item
                break
            if item.kind not in ("noise", "unrecognised", "wait_phase", "recent_note"):
                break
    if head is None:
        return {"reason": None}
    after = seen[seen.index(head) + 1 :]
    phase = next((s.key for s in after if s.kind == "wait_phase"), None)
    notes = [
        {"code": s.group("code"), "detail": s.group("detail")}
        for s in after
        if s.kind == "recent_note"
    ][-3:]
    todo_key = head.group("todo_key")
    return {
        "reason": head.group("reason"),
        "happened_key": head.key,
        "todo_key": todo_key,
        "text": head.text,
        "phase": phase,
        "recent": notes,
        "restore_scene": phase in lex.restore_phases or todo_key in lex.restore_todos,
        "n": head.n,
        "total": head.total,
    }


# ---- the profiles file ----------------------------------------------------------------


_CACHE: list = []


def prompts() -> dict:
    """``aeri_prompts.json`` (read once)."""
    if not _CACHE:
        _CACHE.append(json.loads(PROMPTS_FILE.read_text(encoding="utf-8")))
    return _CACHE[0]


def lexicon(profile_id: str, lang: str = "zh") -> Lexicon:
    data = prompts()
    return Lexicon(data["profiles"][profile_id], data["foreign"], lang)
