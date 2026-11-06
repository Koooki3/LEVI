#!/usr/bin/env python3
"""Static checks of the repository's agent skills (standard library only).

    python3 scripts/check_skills.py [--root <repo root>]

For every ``.claude/skills/<name>/SKILL.md``:
  * the frontmatter exists, ``name`` equals the directory name (lowercase letters, digits, hyphens, at most 64
    characters) and ``description`` is present and at most 1024 characters;
  * a plain (unquoted) ``description`` must not contain ``: `` or `` #`` - strict YAML parsers reject the first and
    cut the value at the second (the first version of the ``autoresearch`` skill failed exactly this way). The fix is
    a ``>-`` block scalar or quotes;
  * when ``ORIGIN.json`` exists, every file it lists exists with the recorded sha256, and no other file is present.
And every ``.agents/skills/<name>`` entry must be a symlink to ``../../.claude/skills/<name>`` that resolves to a
directory holding a ``SKILL.md`` (that is how Codex finds the same files).

It does not execute any skill and does not prove the host loads it: host discovery and behaviour are separate
evidence levels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")
BLOCK_INDICATORS = (">", "|")
QUOTES = ("'", '"')


def parse_frontmatter(text: str) -> tuple[dict[str, tuple[int, str, str]], list[str]]:
    """Returns ({key: (line number, style, value)}, errors). style is 'plain', 'quoted', 'block' or 'mapping'."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return {}, ["line 1: the file must start with a '---' frontmatter line"]
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].rstrip() == "---")
    except StopIteration:
        return {}, ["the frontmatter has no closing '---' line"]
    keys: dict[str, tuple[int, str, str]] = {}
    i = 1
    while i < end:
        line = lines[i]
        m = re.match(r"^([A-Za-z0-9_-]+):(?: +(.*))?$", line)
        if not m:
            i += 1
            continue
        key, value = m.group(1), (m.group(2) or "").rstrip()
        j = i + 1
        cont = []
        while j < end and (
            lines[j].startswith(" ")
            or lines[j].startswith("\t")
            or not lines[j].strip()
        ):
            cont.append(lines[j].strip())
            j += 1
        if value.startswith(BLOCK_INDICATORS):
            keys[key] = (i + 1, "block", " ".join(c for c in cont if c))
        elif value.startswith(QUOTES):
            keys[key] = (i + 1, "quoted", value)
        elif value == "" and cont:
            keys[key] = (i + 1, "mapping", " ".join(c for c in cont if c))
        else:
            keys[key] = (i + 1, "plain", " ".join([value] + [c for c in cont if c]))
        i = j
    return keys, []


def check_skill(skill: Path) -> list[str]:
    errors: list[str] = []
    md = skill / "SKILL.md"
    if not md.is_file():
        return [f"{skill}: no SKILL.md"]
    keys, errs = parse_frontmatter(md.read_text(encoding="utf-8"))
    errors += [f"{md}: {e}" for e in errs]
    if keys:
        name = keys.get("name")
        if not name:
            errors.append(f"{md}: no 'name' in the frontmatter")
        else:
            value = name[2].strip("'\"")
            if value != skill.name:
                errors.append(
                    f"{md}:{name[0]}: name {value!r} must equal the directory name {skill.name!r}"
                )
            if not NAME_RE.match(value):
                errors.append(
                    f"{md}:{name[0]}: name {value!r} must be lowercase letters, digits and hyphens, at most 64"
                )
        desc = keys.get("description")
        if not desc or not desc[2].strip("'\" "):
            errors.append(f"{md}: no non-empty 'description' in the frontmatter")
        else:
            line, style, value = desc
            if len(value) > 1024:
                errors.append(
                    f"{md}:{line}: description is {len(value)} characters (limit 1024)"
                )
            if style == "plain" and (": " in value or " #" in value):
                errors.append(
                    f"{md}:{line}: a plain description contains ': ' or ' #', which strict YAML rejects or truncates; "
                    "use a '>-' block scalar or put the whole value in quotes"
                )
    origin = skill / "ORIGIN.json"
    if origin.is_file():
        try:
            data = json.loads(origin.read_text(encoding="utf-8"))
        except ValueError as exc:
            errors.append(f"{origin}: not valid JSON: {exc}")
            data = {}
        listed = data.get("files") if isinstance(data, dict) else None
        if not isinstance(listed, dict):
            errors.append(f"{origin}: no 'files' map of path to sha256")
        else:
            for rel, digest in sorted(listed.items()):
                path = skill / rel
                if not path.is_file():
                    errors.append(f"{origin}: lists {rel}, which does not exist")
                elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    errors.append(
                        f"{origin}: {rel} does not match its recorded sha256; refresh the record after a reviewed change"
                    )
            actual = {
                str(p.relative_to(skill))
                for p in skill.rglob("*")
                if p.is_file()
                and p.name != "ORIGIN.json"
                and "__pycache__" not in p.parts
            }
            for rel in sorted(actual - set(listed)):
                errors.append(f"{origin}: {rel} exists but is not listed")
    return errors


def check_links(root: Path) -> list[str]:
    errors: list[str] = []
    agents = root / ".agents" / "skills"
    if not agents.is_dir():
        return errors
    for entry in sorted(agents.iterdir()):
        expected = f"../../.claude/skills/{entry.name}"
        if not entry.is_symlink():
            errors.append(f"{entry}: must be a symlink to {expected}")
        elif os.readlink(entry) != expected:
            errors.append(
                f"{entry}: points at {os.readlink(entry)!r}, expected {expected!r}"
            )
        elif not (entry / "SKILL.md").is_file():
            errors.append(f"{entry}: resolves to a directory without SKILL.md")
    return errors


def check_all(root: Path) -> list[str]:
    errors: list[str] = []
    skills = root / ".claude" / "skills"
    if skills.is_dir():
        for skill in sorted(p for p in skills.iterdir() if p.is_dir()):
            errors += check_skill(skill)
    errors += check_links(root)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parent.parent
    )
    args = parser.parse_args(argv)
    errors = check_all(args.root)
    for e in errors:
        print("ERROR", e)
    if errors:
        return 1
    print("skills ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
