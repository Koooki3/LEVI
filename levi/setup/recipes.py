"""Setup recipes: the commands of a machine's operator guide, as a registry
that is checked against the guide.

A recipe names one excerpt of the guide (a Markdown file such as the
maintainer's ``setup.md``) by section number, code block and, optionally, a
line range inside that block, and records the SHA-256 of that excerpt. It
also says what the excerpt touches (robot, GPU, ports), its risk class and
how a user interface may offer it:

========  =============================================================
risk      meaning
========  =============================================================
1         read-only diagnostics
2         services of LEVI, vLLM or the diagnostics recorder
3         the robot control stack or the policy server
4         anything that makes the robot or the gripper move
========  =============================================================

``ui`` is ``native`` (LEVI reads files or kernel tables itself, no script
runs), ``execute`` (the interface may run it, after a person confirms),
``copy`` (shown with a copy button, never run) or ``link``.

The validator refuses a class 3 or 4 recipe marked ``execute`` or
``native``; the only class 3 recipe that may be ``execute`` is a policy
server (``kind = "policy_server"``) that only loads a model and never
commands the robot, and it must carry an explicit ``confirm`` record. A
recipe that connects to a robot-side port is never more than ``copy``.

``levi setup recipes check`` re-reads the guide, finds every excerpt by its
section number and block index, and compares the hash: one changed
character is reported as drift. A section or block that is missing, a
section number that appears twice, a code fence left open, and an excerpt
that moved elsewhere are each reported by name, never guessed around.

The recipe file holds machine-specific paths, addresses and serial numbers,
so it lives outside the repository (``LEVI_SETUP_RECIPES``); so does the
guide (``LEVI_SETUP_DOC``). This module only reads both.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1
RISKS = (1, 2, 3, 4)
UI_MODES = ("native", "execute", "copy", "link")
# Robot-side ports a LEVI process never connects to (the robot servers, the
# SSH tunnel to the second robot, the policy server and the real learner).
NEVER_CONNECT = frozenset({5000, 5001, 5100, 7470, 8000})
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SECTION = re.compile(r"^\d{1,3}(?:\.\d{1,3})*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_RECIPES = 500
MAX_FILE_BYTES = 2 << 20

ENV_RECIPES = "LEVI_SETUP_RECIPES"
ENV_DOC = "LEVI_SETUP_DOC"


# --------------------------------------------------------------- the guide


HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
# A section number has components of at most three digits: "## 2026 notes"
# is a title, not section 2026.
NUMBERED = re.compile(r"^(\d{1,3}(?:\.\d{1,3})*)\.?[ \t]+(.*)$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


@dataclass
class Block:
    index: int  # 1-based within its section
    first: int  # line number of the first content line (1-based)
    lines: list  # content lines, fences excluded

    @property
    def last(self) -> int:
        return self.first + len(self.lines) - 1


@dataclass
class Section:
    number: str | None
    title: str  # heading text without the number
    line: int  # the heading's line
    body: list = field(
        default_factory=list
    )  # (line number, text) until the next heading
    blocks: list = field(default_factory=list)


@dataclass
class Guide:
    sections: list
    problems: list  # malformed structure: a fence left open

    def by_number(self, number: str) -> list:
        return [s for s in self.sections if s.number == number]

    def by_title(self, title: str) -> list:
        return [s for s in self.sections if s.title == title]


def parse_guide(text: str) -> Guide:
    """Split a Markdown guide into headed sections and their fenced blocks.
    Headings inside a code block are code, not headings."""
    lines = split_lines(text.removeprefix("\ufeff"))
    sections = [Section(None, "", 0)]  # text before the first heading
    problems = []
    fence = None  # (char, length, opening line)
    block_lines: list = []
    for number, raw in enumerate(lines, start=1):
        if fence is not None:
            m = FENCE.match(raw)
            if m and m[1][0] == fence[0] and len(m[1]) >= fence[1] and not m[2].strip():
                section = sections[-1]
                section.blocks.append(
                    Block(len(section.blocks) + 1, fence[2] + 1, block_lines)
                )
                section.body.append((number, raw))
                fence, block_lines = None, []
            else:
                block_lines.append(raw)
                sections[-1].body.append((number, raw))
            continue
        m = FENCE.match(raw)
        if m and not (m[1][0] == "`" and "`" in m[2]):
            fence = (m[1][0], len(m[1]), number)
            sections[-1].body.append((number, raw))
            continue
        h = HEADING.match(raw)
        if h:
            title = h[2].strip()
            n = NUMBERED.match(title)
            sections.append(
                Section(n[1] if n else None, (n[2] if n else title).strip(), number)
            )
            continue
        sections[-1].body.append((number, raw))
    if fence is not None:
        problems.append(
            f"code fence opened at line {fence[2]} is never closed: everything "
            "after it would be read as code"
        )
    return Guide(sections, problems)


def split_lines(text: str) -> list:
    """Lines as an editor and ``wc -l`` count them: split at newlines only
    (``str.splitlines`` also splits at U+2028, form feeds and others), a
    trailing carriage return dropped."""
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def normalise(lines) -> str:
    """The text a hash covers: Unicode NFC, trailing spaces removed, leading
    and trailing blank lines removed. Any other change, one character
    included, changes the hash."""
    out = [unicodedata.normalize("NFC", line).rstrip() for line in lines]
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unit(section: Section, block: int | None):
    """``(first line number, lines)`` of a block, or for ``block=None`` of
    the section's whole body up to the next heading (code blocks included,
    leading and trailing blank lines left out); None when the section has no
    such block."""
    if block is None:
        body = list(section.body)
        while body and not body[0][1].strip():
            body.pop(0)
        while body and not body[-1][1].strip():
            body.pop()
        if not body:
            return section.line + 1, []
        return body[0][0], [text for _, text in body]
    if not 1 <= block <= len(section.blocks):
        return None
    b = section.blocks[block - 1]
    return b.first, list(b.lines)


@dataclass
class Excerpt:
    text: str
    first: int
    last: int

    @property
    def sha256(self) -> str:
        return digest(self.text)


def excerpt(section: Section, block: int | None, pick=None) -> Excerpt | None:
    unit = _unit(section, block)
    if unit is None:
        return None
    first, lines = unit
    if pick:
        a, b = pick
        if b > len(lines):
            return None
        lines = lines[a - 1 : b]
        first += a - 1
    return Excerpt(normalise(lines), first, first + max(0, len(lines) - 1))


def _all_excerpts(guide: Guide, size: int | None):
    """Every block (and every window of ``size`` lines inside a block), so a
    moved excerpt can be found again by its hash."""
    for section in guide.sections:
        for block in section.blocks:
            n = len(block.lines)
            spans = (
                [(1, n)]
                if not size
                else [(a, a + size - 1) for a in range(1, n - size + 2)]
            )
            for a, b in spans:
                found = excerpt(section, block.index, (a, b) if size else None)
                if found is not None:
                    yield section, block.index, (a, b) if size else None, found
        prose = excerpt(section, None)
        if prose is not None and not size:
            yield section, None, None, prose


# --------------------------------------------------------------- the recipes


@dataclass(frozen=True)
class Source:
    section: str
    block: int | None  # 1-based code block of the section; None: its whole body
    sha256: str
    heading: str | None = None  # the section's title when recorded
    pick: tuple | None = None  # 1-based inclusive line range inside the block
    lines: tuple | None = None  # where it was when recorded (reference only)
    text: str | None = None  # the recorded excerpt, to show a difference
    # The whole section body (prose around the block included): when set, a
    # change of the surrounding text, a new warning for instance, is drift.
    context_sha256: str | None = None


@dataclass(frozen=True)
class Recipe:
    id: str
    title: str
    risk: int
    ui: str
    source: Source
    kind: str = ""
    requires: tuple = ()
    preconditions: tuple = ()
    robot: bool = False
    gpu: bool = False
    moves: bool = False
    commands_robot: bool = False
    listens: tuple = ()
    connects: tuple = ()
    confirm: dict | None = None
    note: str = ""
    # The touches keys the file wrote down (a policy server must declare
    # robot, moves and commands_robot itself, not inherit the defaults).
    declared: tuple = ()

    @property
    def executable(self) -> bool:
        return self.ui == "execute"

    def public(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "risk": self.risk,
            "ui": self.ui,
            "kind": self.kind,
            "executable": self.executable,
            "requires": list(self.requires),
            "preconditions": list(self.preconditions),
            "touches": {
                "robot": self.robot,
                "gpu": self.gpu,
                "moves": self.moves,
                "commands_robot": self.commands_robot,
                "listens": list(self.listens),
                "connects": list(self.connects),
            },
            "confirm": self.confirm,
            "source": {
                "section": self.source.section,
                "heading": self.source.heading,
                "block": self.source.block,
                "pick": list(self.source.pick) if self.source.pick else None,
                "lines": list(self.source.lines) if self.source.lines else None,
                "sha256": self.source.sha256,
            },
            "note": self.note,
        }


@dataclass
class Book:
    recipes: list
    problems: list  # (recipe id or "", text)
    path: str = ""

    def get(self, recipe_id: str) -> Recipe | None:
        return next((r for r in self.recipes if r.id == recipe_id), None)


class RecipeError(ValueError):
    """The recipe file cannot be read at all."""


class FieldError(ValueError):
    """One recipe has a missing, unknown or badly typed field."""


def _pair(value, what):
    if value is None:
        return None
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or not all(isinstance(x, int) and not isinstance(x, bool) for x in value)
        or not 1 <= value[0] <= value[1]
    ):
        raise FieldError(
            f"{what} must be two line numbers [first, last], first <= last, from 1"
        )
    return (value[0], value[1])


def _ports(value, what):
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 65535
        for p in value
    ):
        raise FieldError(f"{what} must be a list of port numbers")
    return tuple(value)


def _strings(value, what):
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(x, str) for x in value
    ):
        raise FieldError(f"{what} must be a list of strings")
    return tuple(value)


def _flag(value, what):
    if value is None:
        return False
    if not isinstance(value, bool):
        raise FieldError(f"{what} must be true or false")
    return value


KNOWN = {
    "id", "title", "risk", "ui", "source", "kind", "requires", "preconditions",
    "touches", "confirm", "note",
}  # fmt: skip
KNOWN_SOURCE = {
    "section", "heading", "block", "pick", "lines", "sha256", "text", "context_sha256",
}  # fmt: skip
KNOWN_TOUCHES = {"robot", "gpu", "moves", "commands_robot", "listens", "connects"}


def parse_recipe(raw: dict) -> Recipe:
    """One recipe from its table; ValueError names the first bad field."""
    if not isinstance(raw, dict):
        raise FieldError("a recipe must be a table")
    unknown = sorted(set(raw) - KNOWN)
    if unknown:
        raise FieldError(f"unknown field(s) {', '.join(unknown)}")
    rid = raw.get("id")
    if not isinstance(rid, str) or not ID.match(rid):
        raise FieldError("id must be 1-64 letters, digits, '.', '_' or '-'")
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise FieldError("title is required")
    risk = raw.get("risk")
    if risk not in RISKS or isinstance(risk, bool):
        raise FieldError("risk must be 1, 2, 3 or 4")
    ui = raw.get("ui")
    if ui not in UI_MODES:
        raise FieldError(f"ui must be one of {', '.join(UI_MODES)}")
    src = raw.get("source")
    if not isinstance(src, dict):
        raise FieldError("source is required (section, block, sha256)")
    unknown = sorted(set(src) - KNOWN_SOURCE)
    if unknown:
        raise FieldError(f"unknown source field(s) {', '.join(unknown)}")
    section = src.get("section")
    if not isinstance(section, str) or not SECTION.match(section):
        raise FieldError('source.section must be a section number such as "2.2"')
    block = src.get("block")
    if block is not None and (
        not isinstance(block, int) or isinstance(block, bool) or block < 1
    ):
        raise FieldError(
            "source.block must be a code block number from 1 (or absent: the whole section body)"
        )
    sha = src.get("sha256")
    if not isinstance(sha, str) or not SHA256.match(sha):
        raise FieldError("source.sha256 must be 64 lowercase hex digits")
    heading = src.get("heading")
    if heading is not None and not isinstance(heading, str):
        raise FieldError("source.heading must be text")
    text = src.get("text")
    if text is not None:
        if not isinstance(text, str):
            raise FieldError("source.text must be text")
        if digest(normalise(split_lines(text))) != sha:
            raise FieldError("source.text does not have the recorded source.sha256")
    context = src.get("context_sha256")
    if context is not None and (
        not isinstance(context, str) or not SHA256.match(context)
    ):
        raise FieldError("source.context_sha256 must be 64 lowercase hex digits")
    touches = raw.get("touches") or {}
    if not isinstance(touches, dict):
        raise FieldError("touches must be a table")
    unknown = sorted(set(touches) - KNOWN_TOUCHES)
    if unknown:
        raise FieldError(f"unknown touches field(s) {', '.join(unknown)}")
    confirm = raw.get("confirm")
    if confirm is not None and not isinstance(confirm, dict):
        raise FieldError("confirm must be a table")
    kind = raw.get("kind") or ""
    note = raw.get("note") or ""
    if not isinstance(kind, str) or not isinstance(note, str):
        raise FieldError("kind and note must be text")
    return Recipe(
        id=rid,
        title=title.strip(),
        risk=risk,
        ui=ui,
        kind=kind,
        source=Source(
            section=section,
            block=block,
            sha256=sha,
            heading=heading.strip() if isinstance(heading, str) else None,
            pick=_pair(src.get("pick"), "source.pick"),
            lines=_pair(src.get("lines"), "source.lines"),
            text=text,
            context_sha256=context,
        ),
        requires=_strings(raw.get("requires"), "requires"),
        preconditions=_strings(raw.get("preconditions"), "preconditions"),
        robot=_flag(touches.get("robot"), "touches.robot"),
        gpu=_flag(touches.get("gpu"), "touches.gpu"),
        moves=_flag(touches.get("moves"), "touches.moves"),
        commands_robot=_flag(touches.get("commands_robot"), "touches.commands_robot"),
        listens=_ports(touches.get("listens"), "touches.listens"),
        connects=_ports(touches.get("connects"), "touches.connects"),
        confirm=dict(confirm) if confirm is not None else None,
        note=note,
        declared=tuple(sorted(touches)) if "touches" in raw else (),
    )


def safety_problems(recipe: Recipe) -> list:
    """Why the interface may not offer this recipe as marked (empty: it may).

    Class 3 and 4 recipes are never run by the interface and are not
    ``native`` either (nothing about them is LEVI reading a file). The one
    exception is a class 3 policy server that only loads a model: it may be
    ``execute``, with an explicit ``confirm`` record. Any policy server
    (whatever its ``ui``) must write down ``touches.robot``, ``moves`` and
    ``commands_robot`` as false itself, connect to nothing and listen on no
    robot-side port; any executable recipe stays off the robot."""
    out = []
    if recipe.moves and recipe.risk != 4:
        out.append("touches.moves is true, so risk must be 4")
    if recipe.commands_robot and recipe.risk < 3:
        out.append("touches.commands_robot is true, so risk must be 3 or 4")
    robot_ports = sorted(set(recipe.connects) & NEVER_CONNECT)
    if robot_ports and recipe.ui not in ("copy", "link"):
        out.append(
            f"connects to robot-side port(s) {robot_ports}: only copy or link, "
            "LEVI never connects there"
        )
    if recipe.risk == 4 and recipe.ui in ("execute", "native"):
        out.append(
            f"risk 4 (makes the robot move) cannot be {recipe.ui}: only copy or link"
        )
    if recipe.risk == 3 and recipe.ui == "native":
        out.append("risk 3 (robot control stack or policy server) cannot be native")
    if recipe.risk == 3 and recipe.ui == "execute":
        if recipe.kind != "policy_server":
            out.append(
                "risk 3 cannot be execute: only a policy server (kind = "
                '"policy_server") may be, and only with a confirm record'
            )
        else:
            if recipe.moves or recipe.commands_robot or recipe.robot:
                out.append(
                    "an executable policy server must only load a model: "
                    "touches.robot, moves and commands_robot must be false"
                )
            confirm = recipe.confirm or {}
            if (
                confirm.get("required") is not True
                or not str(confirm.get("decision") or "").strip()
            ):
                out.append(
                    "an executable policy server needs confirm = { required = true, "
                    'decision = "<who allowed it, when>" }'
                )
    if recipe.ui == "execute" and recipe.risk in (1, 2) and recipe.moves:
        out.append("an executable recipe cannot move the robot")
    if recipe.kind == "policy_server":
        out += _model_only_problems(recipe)
    if recipe.ui == "execute":
        if recipe.robot:
            out.append("an executable recipe must have touches.robot = false")
        allowed = {8000} if recipe.kind == "policy_server" else set()
        listened = sorted(set(recipe.listens) & (NEVER_CONNECT - allowed))
        if listened:
            out.append(f"an executable recipe listens on robot-side port(s) {listened}")
    return out


MODEL_ONLY_KEYS = ("robot", "moves", "commands_robot")


def _model_only_problems(recipe: Recipe) -> list:
    """A policy server only loads a model (the user's decision for the D
    step): said explicitly, not inherited from defaults."""
    out = []
    absent = [k for k in MODEL_ONLY_KEYS if k not in recipe.declared]
    if absent:
        out.append(
            "a policy server must declare touches "
            + ", ".join(f"{k} = false" for k in MODEL_ONLY_KEYS)
            + f" (missing: {', '.join(absent)})"
        )
    if recipe.robot or recipe.moves or recipe.commands_robot:
        out.append(
            "a policy server must only load a model: touches.robot, moves and "
            "commands_robot must be false"
        )
    if recipe.connects:
        out.append(
            f"a policy server connects to nothing (touches.connects {list(recipe.connects)})"
        )
    listened = sorted(set(recipe.listens) & (NEVER_CONNECT - {8000}))
    if listened:
        out.append(f"a policy server cannot listen on robot-side port(s) {listened}")
    return out


def load(path) -> Book:
    """Read and validate a recipe file (TOML, or JSON for ``.json``).

    A file that cannot be parsed raises RecipeError; problems with single
    recipes (bad fields, safety, duplicate ids, unknown or circular
    ``requires``) are listed in ``Book.problems`` and those recipes are left
    out of ``Book.recipes``, so nothing unsafe is ever offered."""
    path = Path(path).expanduser()
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise RecipeError(f"recipe file is larger than {MAX_FILE_BYTES} bytes")
        raw = path.read_bytes()
    except OSError as exc:
        raise RecipeError(
            f"cannot read the recipe file: {exc.strerror or exc}"
        ) from exc
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(raw.decode("utf-8"))
        else:
            data = tomllib.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RecipeError(f"cannot parse the recipe file: {exc}") from exc
    return from_data(data, str(path))


def from_data(data, path="") -> Book:
    if not isinstance(data, dict):
        raise RecipeError("the recipe file must be a table with a `recipe` list")
    version = data.get("version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise RecipeError(f"recipe file version {version!r} is not {SCHEMA_VERSION}")
    items = data.get("recipe", [])
    if not isinstance(items, list):
        raise RecipeError("`recipe` must be a list of tables ([[recipe]])")
    if len(items) > MAX_RECIPES:
        raise RecipeError(f"more than {MAX_RECIPES} recipes")
    problems, recipes, seen = [], [], set()
    for position, item in enumerate(items, start=1):
        rid = item.get("id") if isinstance(item, dict) else None
        label = rid if isinstance(rid, str) else f"#{position}"
        try:
            recipe = parse_recipe(item)
        except ValueError as exc:
            problems.append((label, str(exc)))
            continue
        if recipe.id in seen:
            problems.append((recipe.id, "duplicate id"))
            continue
        seen.add(recipe.id)
        unsafe = safety_problems(recipe)
        if unsafe:
            problems += [(recipe.id, text) for text in unsafe]
            continue
        recipes.append(recipe)
    # A recipe whose requirement is unknown, refused or itself left out is
    # left out too (to a fixed point), and so is every recipe on a cycle: a
    # wizard must never offer a step whose prerequisites cannot be met.
    kept = list(recipes)
    for cycle in _cycles(kept):
        problems.append((cycle[0], "requires form a cycle: " + " -> ".join(cycle)))
    on_cycle = {node for cycle in _cycles(kept) for node in cycle}
    problems += [(rid, "left out: on a requires cycle") for rid in sorted(on_cycle)]
    kept = [r for r in kept if r.id not in on_cycle]
    while True:
        known = {r.id for r in kept}
        dropped = []
        for recipe in kept:
            missing = [x for x in recipe.requires if x not in known]
            if missing:
                dropped.append(recipe)
                problems.append(
                    (
                        recipe.id,
                        (
                            "left out: requires unknown, refused or left-out "
                            f"recipe(s) {', '.join(missing)}"
                        ),
                    )
                )
        if not dropped:
            break
        kept = [r for r in kept if r not in dropped]
    return Book(kept, problems, path)


def _cycles(recipes) -> list:
    graph = {r.id: [x for x in r.requires] for r in recipes}
    state, found = {}, []

    def visit(node, trail):
        state[node] = 1
        for nxt in graph.get(node, []):
            if nxt not in graph:
                continue
            if state.get(nxt) == 1:
                found.append(trail[trail.index(nxt) :] + [nxt])
            elif not state.get(nxt):
                visit(nxt, trail + [nxt])
        state[node] = 2

    for node in graph:
        if not state.get(node):
            visit(node, [node])
    return found


# --------------------------------------------------------------- the check


@dataclass
class Finding:
    id: str
    status: str  # ok | drift | moved | missing | ambiguous | doc_error
    reasons: list = field(default_factory=list)
    lines_now: tuple | None = None
    shifted: bool = False  # ok, but no longer at the recorded line numbers
    found_at: dict | None = None  # where a moved excerpt is now
    diff: str = ""

    def public(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "reasons": self.reasons,
            "lines_now": list(self.lines_now) if self.lines_now else None,
            "shifted": self.shifted,
            "found_at": self.found_at,
            "diff": self.diff,
        }


def _locate(guide: Guide, source: Source):
    """(section, problem) for a recipe's source."""
    sections = guide.by_number(source.section)
    if len(sections) > 1:
        lines = ", ".join(str(s.line) for s in sections)
        return None, (
            "ambiguous",
            f"section {source.section} appears more than once (lines {lines})",
        )
    if sections:
        return sections[0], None
    if source.heading:
        titled = guide.by_title(source.heading)
        if len(titled) == 1:
            text = (
                f"section {source.section} is gone; a section titled "
                f"{source.heading!r} is now {titled[0].number or 'unnumbered'} "
                f"(line {titled[0].line})"
            )
            return None, ("missing", text)
    return None, ("missing", f"section {source.section} is not in the guide")


def _search(guide: Guide, source: Source) -> dict | None:
    size = None
    if source.pick:
        size = source.pick[1] - source.pick[0] + 1
    for section, block, pick, found in _all_excerpts(guide, size):
        if found.sha256 == source.sha256:
            return {
                "section": section.number,
                "heading": section.title,
                "block": block,
                "pick": list(pick) if pick else None,
                "lines": [found.first, found.last],
            }
    return None


def check_recipe(guide: Guide, recipe: Recipe) -> Finding:
    source = recipe.source
    if guide.problems:
        return Finding(recipe.id, "doc_error", list(guide.problems))
    section, problem = _locate(guide, source)
    if section is None:
        status, text = problem
        finding = Finding(recipe.id, status, [text])
        moved = _search(guide, source) if status == "missing" else None
        if moved:
            finding.status, finding.found_at = "moved", moved
            finding.reasons.append(
                "the same text is now at section {section} block {block} lines "
                "{lines[0]}-{lines[1]}: update the recipe's source".format(**moved)
            )
        return finding
    reasons = []
    if source.heading and source.heading != section.title:
        reasons.append(
            f"section {source.section} heading changed: {source.heading!r} -> {section.title!r}"
        )
    found = excerpt(section, source.block, source.pick)
    if found is None:
        have = len(section.blocks)
        what = (
            f"section {source.section} has {have} code block(s), the recipe names block {source.block}"
            if source.block and source.block > have
            else f"block {source.block} of section {source.section} has fewer lines than {list(source.pick)}"
        )
        finding = Finding(recipe.id, "missing", [*reasons, what])
        moved = _search(guide, source)
        if moved:
            finding.status, finding.found_at = "moved", moved
            finding.reasons.append(
                "the same text is now at section {section} block {block} lines "
                "{lines[0]}-{lines[1]}: update the recipe's source".format(**moved)
            )
        return finding
    lines_now = (found.first, found.last)
    if found.sha256 != source.sha256:
        finding = Finding(recipe.id, "drift", reasons, lines_now)
        moved = _search(guide, source)
        if moved:
            finding.status, finding.found_at = "moved", moved
            finding.reasons.append(
                f"section {source.section} block {source.block} now holds other text; "
                "the recorded text is at section {section} block {block} lines "
                "{lines[0]}-{lines[1]}: update the recipe's source".format(**moved)
            )
        else:
            finding.reasons.append(
                f"the text at section {source.section} block {source.block} "
                f"(lines {lines_now[0]}-{lines_now[1]}) changed: review it before "
                "anyone copies this command"
            )
            if source.text is not None:
                finding.diff = "\n".join(
                    difflib.unified_diff(
                        normalise(split_lines(source.text)).split("\n"),
                        found.text.split("\n"),
                        "recorded",
                        "guide now",
                        lineterm="",
                    )
                )
        return finding
    if source.context_sha256:
        around = excerpt(section, None)
        if around is None or around.sha256 != source.context_sha256:
            reasons.append(
                f"the text around the excerpt in section {source.section} changed "
                "(a new warning or condition, perhaps): review it"
            )
    if reasons:
        return Finding(recipe.id, "drift", reasons, lines_now)
    shifted = bool(source.lines and tuple(source.lines) != lines_now)
    return Finding(recipe.id, "ok", [], lines_now, shifted)


def check(book: Book, guide_text: str) -> list:
    guide = parse_guide(guide_text)
    return [check_recipe(guide, recipe) for recipe in book.recipes]


# --------------------------------------------------------------- command line


def _paths(args):
    recipes = args.recipes or os.environ.get(ENV_RECIPES, "").strip()
    doc = args.doc or os.environ.get(ENV_DOC, "").strip()
    return recipes, doc


def _read_doc(doc: str) -> str:
    try:
        # utf-8-sig: a byte order mark must not hide the first heading.
        return Path(doc).expanduser().read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecipeError(f"cannot read the guide: {exc}") from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="levi setup recipes",
        description=(
            "Setup recipes: check a recipe file against the operator guide "
            "(read-only). Exit 0: every recipe valid and in step; 1: problems "
            "or drift; 2: a file cannot be read."
        ),
    )
    sub = parser.add_subparsers(dest="action", required=True)
    chk = sub.add_parser(
        "check", help="validate the recipes and compare each excerpt's hash"
    )
    chk.add_argument("--recipes", help=f"recipe file (default ${ENV_RECIPES})")
    chk.add_argument("--doc", help=f"the guide, a Markdown file (default ${ENV_DOC})")
    chk.add_argument("--json", action="store_true", help="machine-readable output")
    ex = sub.add_parser(
        "excerpt",
        help="print an excerpt of the guide with its hash and lines (to write a recipe)",
    )
    ex.add_argument("--doc", help=f"the guide (default ${ENV_DOC})")
    ex.add_argument("--section", required=True)
    ex.add_argument(
        "--block",
        type=int,
        help="code block number from 1 (absent: the whole section body)",
    )
    ex.add_argument("--pick", type=int, nargs=2, metavar=("FIRST", "LAST"))
    args = parser.parse_args(argv)
    try:
        if args.action == "excerpt":
            args.recipes = None
            _, doc = _paths(args)
            if not doc:
                parser.error(f"no guide: pass --doc or set {ENV_DOC}")
            guide = parse_guide(_read_doc(doc))
            for text in guide.problems:
                print(f"guide: {text}", file=sys.stderr)
            if guide.problems:
                return 1
            sections = guide.by_number(args.section)
            if len(sections) != 1:
                print(
                    f"section {args.section} appears {len(sections)} times",
                    file=sys.stderr,
                )
                return 1
            pick = tuple(args.pick) if args.pick else None
            if pick and not 1 <= pick[0] <= pick[1]:
                parser.error("--pick FIRST LAST with 1 <= FIRST <= LAST")
            found = excerpt(sections[0], args.block, pick)
            if found is None:
                print("no such block or lines in that section", file=sys.stderr)
                return 1
            print(
                json.dumps(
                    {
                        "section": args.section,
                        "heading": sections[0].title,
                        "block": args.block,
                        "pick": list(pick) if pick else None,
                        "lines": [found.first, found.last],
                        "sha256": found.sha256,
                        "context_sha256": excerpt(sections[0], None).sha256,
                        "text": found.text,
                    },
                    ensure_ascii=False,
                    indent=1,
                )
            )
            return 0
        recipes, doc = _paths(args)
        if not recipes:
            parser.error(f"no recipe file: pass --recipes or set {ENV_RECIPES}")
        if not doc:
            parser.error(f"no guide: pass --doc or set {ENV_DOC}")
        book = load(recipes)
        findings = check(book, _read_doc(doc))
    except RecipeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    bad = [f for f in findings if f.status != "ok"]
    if args.json:
        print(
            json.dumps(
                {
                    "ok": not bad and not book.problems,
                    "recipes": len(book.recipes),
                    "problems": [{"id": i, "problem": t} for i, t in book.problems],
                    "findings": [f.public() for f in findings],
                },
                ensure_ascii=False,
                indent=1,
            )
        )
    else:
        for rid, text in book.problems:
            print(f"refused {rid}: {text}")
        for f in findings:
            mark = "ok" if f.status == "ok" else f.status.upper()
            extra = " (lines shifted)" if f.shifted else ""
            print(f"{mark:9} {f.id}{extra}")
            for text in f.reasons:
                print(f"          {text}")
            if f.diff:
                print("\n".join("          " + line for line in f.diff.splitlines()))
        print(
            f"{len(findings) - len(bad)} of {len(findings)} recipe(s) in step; "
            f"{len(book.problems)} problem(s) in the recipe file"
        )
    return 1 if bad or book.problems else 0
