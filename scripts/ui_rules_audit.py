"""Read-only audit of src/ against the mechanically checkable UI rules.

Sources (docs/DESIGN.md, "Interaction rules"): ui-ux-pro-max quick-reference,
Vercel's web-interface-guidelines, WCAG 2.2 target size and focus appearance.

  python3 scripts/ui_rules_audit.py            # report; exit 1 on a hard-rule hit
  python3 scripts/ui_rules_audit.py --all      # also list the report-only rules

Hard rules (a hit fails the audit and tests/test_ui_rules_audit.py):
  anim-all          `transition: all` (list the properties)
  click-div         onClick on a <div>/<span> without a role (use <button>/<a>)
  zoom-disabled     user-scalable=no / maximum-scale=1
  tiny-text         a literal font-size under 12px in CSS
Report-only rules: outline-none (needs a visible replacement), locale-less
toLocale*String() (use the language the person chose).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HARD = {
    "anim-all": "transition: all (list the properties)",
    "click-div": "onClick on a div/span without a role (use button or a)",
    "zoom-disabled": "zoom disabled in the viewport",
    "tiny-text": "literal font-size under 12px",
}
SOFT = {
    "outline-none": "outline: none (needs a visible replacement)",
    "locale-less": "toLocale*String() without the chosen language",
}


def audit_text(name: str, text: str) -> list[tuple[str, str, int, str]]:
    """Findings (rule, file, line, excerpt) of one file's text."""
    found: list[tuple[str, str, int, str]] = []
    css = name.endswith(".css")
    for number, line in enumerate(text.split("\n"), 1):
        rules: list[str] = []
        if css:
            if re.search(r"transition:\s*all\b", line):
                rules.append("anim-all")
            size = re.search(r"font-size:\s*(\d+(?:\.\d+)?)px", line)
            if size and float(size.group(1)) < 12:
                rules.append("tiny-text")
            if re.search(r"outline:\s*(none|0)\b", line):
                rules.append("outline-none")
        else:
            if re.search(r"<(div|span)\b[^>]*\bonClick=", line) and "role=" not in line:
                rules.append("click-div")
            if "user-scalable=no" in line or "maximum-scale=1" in line:
                rules.append("zoom-disabled")
            if re.search(r"toLocale(Time|Date)?String\(\)", line):
                rules.append("locale-less")
        found += [(rule, name, number, line.strip()[:110]) for rule in rules]
    return found


def audit_tree(src: Path) -> list[tuple[str, str, int, str]]:
    found: list[tuple[str, str, int, str]] = []
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.suffix not in (".tsx", ".ts", ".css"):
            continue
        if "__tests__" in path.parts:
            continue
        found += audit_text(
            str(path.relative_to(src.parent)), path.read_text(errors="ignore")
        )
    return found


def main(argv: list[str]) -> int:
    show_all = "--all" in argv
    root = Path(__file__).resolve().parent.parent
    found = audit_tree(root / "src")
    hard = [f for f in found if f[0] in HARD]
    for rule, text in {**HARD, **(SOFT if show_all else {})}.items():
        hits = [f for f in found if f[0] == rule]
        if not hits and not show_all:
            continue
        print(f"{rule}: {len(hits)}  ({text})")
        for _, name, number, excerpt in hits[:8]:
            print(f"    {name}:{number}: {excerpt}")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
