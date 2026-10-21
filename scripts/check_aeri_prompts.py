"""Read-only drift check of the words and keys the hosted AERI terminal D depends on.

    python3 scripts/check_aeri_prompts.py --worktree <the AERI worktree> [--profile ID] [--sample <screen file>]

``levi/marvel0/aeri_screen.py`` tells terminal D's prompts apart by their words,
and ``levi/marvel0/aeri_state.py`` decides which key to offer from them.
``levi/marvel0/aeri_prompts.json`` holds those words and key tables for each AERI
code version the page knows (a profile), copied from the AERI sources by
``tests/marvel0/regen_aeri_prompts.py``. This script checks one worktree against
one profile and says **what changed** instead of "no": the sha256 of the four
source files, every sentence of ``console_text.py`` in both languages, the key
tables and the wait tables of ``console.py``, and the source snippets that pin the
order of the prompts. A sentence that moved is reported with its new text; one
that is gone fails the check, and so does a file that cannot be read.

With ``--sample`` (a screen file, e.g. a recording of a real run) it also
classifies every line the way the page does and lists the lines nobody knows:
the first real screen of a new AERI version is read this way.

Nothing is written, imported from AERI or run: the AERI files are only read, as
text, and parsed with ``ast`` (``--sample`` imports ``aeri_screen``, which is
pure). Run it by hand before the first hosted run of a day and after any change
to the AERI code. Exit 0: the worktree is what the profile was written from.
Exit 1: it differs (or a line of the sample is unknown). Exit 2: a file, the
profile or the prompts file cannot be read.

A worktree that differs is not hosted: ``profile_known`` and the code pin of the
readiness refuse it. Update the profile (regenerate the file, review the diff)
before the page may host that code."""

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "levi" / "marvel0" / "aeri_prompts.json"
SKIP_PREFIXES = ("scene_", "label_", "legacy_", "target_", "plain_", "v_")
TABLES = (
    "ENTER",
    "QUIT",
    "VERDICT_KEYS",
    "CONFIRM_KEYS",
    "CANCEL_KEYS",
    "WAIT_REASONS",
    "SUBTASK_WAIT_NOTES",
    "CONFIRM_REASONS",
    "ROUTINE_WAITS",
)
# the profile's table name -> the console.py table it was copied from
TABLE_OF = {
    "enter": "ENTER",
    "quit": "QUIT",
    "verdict": "VERDICT_KEYS",
    "confirm": "CONFIRM_KEYS",
    "cancel": "CANCEL_KEYS",
}


def literal(node):
    """A table of console.py: a literal, or ``frozenset({...})`` of one."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
    ):
        return sorted(ast.literal_eval(node.args[0]))
    value = ast.literal_eval(node)
    return sorted(value) if isinstance(value, (set, frozenset)) else value


def assignments(source: str, names) -> dict:
    """The module-level literal assignments of ``names`` in ``source``."""
    found = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
            if name in names:
                found[name] = literal(node.value)
    return found


def lines_of(text: str, needle: str) -> list:
    """The 1-based lines the needle starts on."""
    found, at = [], text.find(needle)
    while at >= 0:
        found.append(text.count("\n", 0, at) + 1)
        at = text.find(needle, at + 1)
    return found


def read(root: Path, rel: str):
    try:
        return (root / rel).read_text(encoding="utf-8")
    except OSError:
        return None


def check(root: Path, profile: dict) -> dict:
    """``{"unreadable", "digest", "changed", "missing", "added", "tables",
    "code"}``: each a list of findings (empty: fine)."""
    out = {
        k: []
        for k in (
            "unreadable",
            "digest",
            "changed",
            "missing",
            "added",
            "tables",
            "code",
        )
    }
    sources = {}
    for rel, want in sorted(profile["sources_sha256"].items()):
        text = read(root, rel)
        if text is None:
            out["unreadable"].append(rel)
            continue
        sources[rel] = text
        if hashlib.sha256(text.encode()).hexdigest() != want:
            out["digest"].append(rel)
    console_text = sources.get("levi/automatic/console_text.py")
    if console_text is not None:
        try:
            now = assignments(console_text, {"TEXT"})["TEXT"]
        except (SyntaxError, KeyError, ValueError):
            out["unreadable"].append("levi/automatic/console_text.py (TEXT)")
            now = {}
        for lang, texts in profile["texts"].items():
            for key, text in texts.items():
                if key not in now.get(lang, {}):
                    out["missing"].append(f"{lang}.{key}")
                elif now[lang][key] != text:
                    out["changed"].append(f"{lang}.{key}: now {now[lang][key]!r}")
            for key in now.get(lang, {}):
                if key not in texts and not key.startswith(SKIP_PREFIXES):
                    out["added"].append(f"{lang}.{key}")
    console = sources.get("levi/automatic/console.py")
    if console is not None:
        try:
            tables = assignments(console, set(TABLES))
        except (SyntaxError, ValueError):
            tables = {}
            out["unreadable"].append("levi/automatic/console.py (tables)")
        for name, table in TABLE_OF.items():
            have = profile["keys"][name]
            got = tables.get(table)
            if got is None:
                out["tables"].append(f"{table}: gone")
            elif (list(got) if name != "verdict" else got) != have:
                out["tables"].append(f"{table}: now {got!r}")
        for name, table in (
            ("wait_reasons", "WAIT_REASONS"),
            ("wait_notes", "SUBTASK_WAIT_NOTES"),
            ("confirm_reasons", "CONFIRM_REASONS"),
            ("routine_waits", "ROUTINE_WAITS"),
        ):
            got = tables.get(table)
            have = profile[name]
            if name == "wait_reasons" and got is not None:
                got = {k: list(v) for k, v in got.items()}
            if got != have:
                out["tables"].append(f"{table}: differs")
    for entry in profile["code"]:
        text = sources.get(entry["file"])
        if text is None:
            continue
        if not lines_of(text, entry["source"]):
            out["code"].append(f"{entry['file']}: {entry['source']!r} is gone")
    return out


def unknown_lines(sample: Path, profile_id: str, lang: str) -> list:
    """The lines of a screen file that the page cannot place."""
    sys.path.insert(0, str(ROOT))
    from levi.marvel0 import aeri_screen

    lex = aeri_screen.lexicon(profile_id, lang)
    buf = aeri_screen.ScreenBuf()
    buf.feed(sample.read_bytes())
    view = aeri_screen.read(buf, lex)
    return [s.text for s in view.seen if s.kind in ("unrecognised", "partial")]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--worktree", type=Path, required=True, help="the AERI worktree"
    )
    parser.add_argument(
        "--profile", help="a profile of aeri_prompts.json (default: its default)"
    )
    parser.add_argument("--lang", default="zh", help="the language of --sample")
    parser.add_argument("--sample", type=Path, help="a recording of a run's screen")
    args = parser.parse_args(argv)
    try:
        data = json.loads(PROMPTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("the prompts file cannot be read", file=sys.stderr)
        return 2
    profile_id = args.profile or data["default"]
    profile = data["profiles"].get(profile_id)
    if profile is None:
        print(
            f"unknown profile {profile_id!r} (known: {', '.join(sorted(data['profiles']))})",
            file=sys.stderr,
        )
        return 2
    if not args.worktree.is_dir():
        print("the worktree is not a folder", file=sys.stderr)
        return 2
    found = check(args.worktree, profile)
    bad = 0
    for kind in (
        "unreadable",
        "digest",
        "missing",
        "changed",
        "added",
        "tables",
        "code",
    ):
        for item in found[kind]:
            print(f"{kind.upper():10} {item}")
            bad += 1
    if args.sample is not None:
        try:
            unknown = unknown_lines(args.sample, profile_id, args.lang)
        except OSError:
            print("the sample cannot be read", file=sys.stderr)
            return 2
        for text in unknown:
            print(f"UNKNOWN    {text[:140]}")
            bad += 1
    print(
        f"profile {profile_id}: "
        + (
            "the worktree is what it was written from"
            if not bad
            else f"{bad} difference(s)"
        )
    )
    if found["unreadable"]:
        return 2
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
