"""Read-only drift check of the strings the hosted evaluation run depends on.

``levi/marvel0/run.py`` decides what the demo client is doing from lines the
client prints and from its session file, and chooses which keys to press from
what the client does with them. ``tests/marvel0/fixtures/client_prompts.json``
lists those strings (with the file and line they were read at); this script
checks that each is still in the client's source. A string that moved is
reported with its new line; one that is gone fails the check, and so does a
client file that cannot be read.

    python3 scripts/check_client_prompts.py [--root <folder of the client files>]

Nothing is written, imported or run: the client files are only read. Run it by
hand before the first hosted run of a day, and after any change to the client.
The default folder is ``examples/fr3_local`` of the ``openpi`` folder next to
the LEVI checkout (found from this file's place, also from a worktree).
Exit 0: every string is there. Exit 1: one is missing or a file is not
readable. Exit 2: the folder or the fixture is not found.
"""

import argparse
import json
import sys
from pathlib import Path

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "marvel0"
    / "fixtures"
    / "client_prompts.json"
)
RELATIVE = ("openpi", "examples", "fr3_local")


def find_root() -> Path | None:
    """The client folder, looking at every parent of this file for an
    ``openpi`` folder beside it."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent.joinpath(*RELATIVE)
        if candidate.is_dir():
            return candidate
    return None


def lines_of(text: str, needle: str) -> list:
    """The 1-based lines the needle starts on."""
    found, at = [], text.find(needle)
    while at >= 0:
        found.append(text.count("\n", 0, at) + 1)
        at = text.find(needle, at + 1)
    return found


def check(root: Path, prompts: dict) -> tuple:
    """``(missing, moved, ok)`` lists of ``(id, file, line, found_line)``."""
    cache: dict = {}
    missing, moved, ok = [], [], []
    for key, entry in prompts.items():
        name = entry["file"]
        if name not in cache:
            try:
                cache[name] = (root / name).read_text(encoding="utf-8")
            except OSError:
                cache[name] = None
        text = cache[name]
        lines = [] if text is None else lines_of(text, entry["source"])
        hint = entry.get("line")
        found = hint if hint in lines else (lines[0] if lines else None)
        row = (key, name, hint, found)
        if found is None:
            missing.append(row)
        elif found != hint:
            moved.append(row)
        else:
            ok.append(row)
    return missing, moved, ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, help="folder of the client files")
    args = parser.parse_args(argv)
    root = args.root or find_root()
    if root is None or not Path(root).is_dir():
        print("the client folder is not found; pass --root", file=sys.stderr)
        return 2
    try:
        prompts = json.loads(FIXTURE.read_text(encoding="utf-8"))["prompts"]
    except (OSError, ValueError, KeyError):
        print("the prompt fixture cannot be read", file=sys.stderr)
        return 2
    missing, moved, ok = check(Path(root), prompts)
    for key, name, line, found in moved:
        print(f"moved   {key}: {name} line {line} -> {found}")
    for key, name, line, _ in missing:
        print(f"MISSING {key}: {name} (was line {line})")
    print(f"{len(ok)} in place, {len(moved)} moved, {len(missing)} missing")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
