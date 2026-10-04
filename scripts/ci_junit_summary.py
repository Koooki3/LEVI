"""Markdown summary of the failed tests in a pytest JUnit XML file.

CI appends the output to ``$GITHUB_STEP_SUMMARY`` so the names of failing
tests are visible on the run page without downloading logs. Standard library
only: it runs with the runner's system Python, before or without the venv.
"""

import html
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_CASES = 50
MAX_MESSAGE = 300


def cell(text: str, code: bool = False) -> str:
    """One table cell: no column breaks, no code-span ends, no HTML tags.

    Inside a code span HTML is shown as typed, so only plain cells are escaped.
    """
    if not code:
        text = html.escape(text, quote=False)
    return text.replace("|", "\\|").replace("`", "'")


def summary(path: Path) -> str:
    if not path.is_file():
        return f"### pytest\n\nNo JUnit file at `{path}` (pytest did not finish).\n"
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    bad: list[tuple[str, str, str]] = []
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.get(key, 0) or 0)
        for case in suite.iter("testcase"):
            for kind in ("failure", "error"):
                node = case.find(kind)
                if node is None:
                    continue
                name = f"{case.get('classname', '')}::{case.get('name', '')}"
                message = (node.get("message") or node.text or "").strip()
                first = message.splitlines()[0] if message else ""
                bad.append((kind, name, first[:MAX_MESSAGE]))
    lines = [
        "### pytest",
        "",
        "{tests} tests, {failures} failed, {errors} errors, {skipped} skipped".format(
            **totals
        ),
        "",
    ]
    if bad:
        lines += ["| | test | message |", "|---|---|---|"]
        for kind, name, first in bad[:MAX_CASES]:
            lines.append(f"| {kind} | `{cell(name, code=True)}` | {cell(first)} |")
        if len(bad) > MAX_CASES:
            lines.append(f"\n{len(bad) - MAX_CASES} more not shown.")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.stdout.write(summary(Path(sys.argv[1])))
