#!/usr/bin/env python3
"""Plain text from an HTML paper (arXiv HTML / LaTeXML). Standard library only.

    python3 -I html2txt.py <in.html> <out.txt>     # headings (h1-h4) become "## " lines
    python3 -I html2txt.py --selftest

Dropped: script, style, template, noscript, nav, header, footer, svg and button elements, anything with the
``hidden`` attribute or ``display:none`` / ``visibility:hidden`` in its style. MathML is replaced by its
``alttext`` (the TeX source) when it has one. If the input ends inside a dropped element, everything after it
was dropped too and a warning goes to stderr: look at the HTML around it before trusting the text.
Indentation inside <pre> is not preserved.

The input is untrusted data: it is only parsed, never executed. ``-I`` removes the script's directory,
``PYTHON*`` variables and user site-packages from the import path, so nothing next to the script or in the
environment can shadow a standard-library module.
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser

SKIP = {
    "script",
    "style",
    "template",
    "noscript",
    "nav",
    "header",
    "footer",
    "svg",
    "button",
}
VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
BLOCK = {
    "p",
    "div",
    "section",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "li",
    "tr",
    "table",
    "figcaption",
    "br",
    "pre",
    "blockquote",
}
HEADING = {"h1", "h2", "h3", "h4"}


def _hidden(attrs) -> bool:
    a = dict(attrs)
    if "hidden" in a:
        return True
    style = (a.get("style") or "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip_tag: str | None = None  # the element whose content is being dropped
        self.depth = 0  # nesting of that same tag name while dropping

    def _begin_skip(self, tag: str) -> None:
        self.skip_tag, self.depth = tag, 1

    def handle_starttag(self, tag, attrs):
        if self.skip_tag:
            if tag == self.skip_tag and tag not in VOID:
                self.depth += 1
            return
        if tag == "math":
            alt = dict(attrs).get("alttext")
            if alt:
                self.out.append(" " + alt + " ")
            self._begin_skip(tag)
            return
        if tag in SKIP or (tag not in VOID and _hidden(attrs)):
            self._begin_skip(tag)
            return
        if tag in BLOCK:
            self.out.append("\n")
        if tag in HEADING:
            self.out.append("\n## ")

    def handle_endtag(self, tag):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.depth -= 1
                if self.depth == 0:
                    self.skip_tag = None
            return
        if tag in BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip_tag:
            self.out.append(data)


def html_to_text(html: str) -> tuple[str, list[str]]:
    """Returns (text, warnings)."""
    parser = _Text()
    parser.feed(html)
    parser.close()
    warnings = []
    if parser.skip_tag:
        warnings.append(
            f"input ended inside a dropped <{parser.skip_tag}> element: everything after it was dropped"
        )
    text = "".join(parser.out)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(
        r"(?m)^## *\n+", "## ", text
    )  # a heading whose text starts on the next line
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip() + "\n", warnings


def _check(cond: bool, message: str) -> None:
    if not cond:
        print("selftest FAILED:", message, file=sys.stderr)
        raise SystemExit(1)


def _selftest() -> int:
    text, warns = html_to_text(
        "<html><head><style>p{color:red}</style><script>alert(1)</script></head><body>"
        "<nav>menu</nav><h2>2 Method</h2><p>First  sentence.</p><div>Second<br>line</div>"
        "<button>click</button><footer>foot</footer></body></html>"
    )
    _check("## 2 Method" in text, text)
    _check("First sentence." in text, text)
    for gone in ("alert", "color:red", "menu", "click", "foot"):
        _check(gone not in text, f"{gone!r} leaked: {text!r}")
    _check("Second" in text and "line" in text, text)
    _check(
        text.endswith("\n") and not text.startswith("\n") and not text.endswith("\n\n"),
        repr(text),
    )
    _check(warns == [], warns)

    text, _ = html_to_text(
        '<p>Loss <math alttext="x_t=\\epsilon"><mi>x</mi><msub><mi>x</mi><mi>t</mi></msub></math> ok</p>'
    )
    _check("x_t=\\epsilon" in text and "xtx" not in text, text)

    text, _ = html_to_text(
        "<p>seen</p><template><p>tmpl</p></template><noscript>ns</noscript>"
        '<div hidden>secret<div>inner</div>still</div><p style="display: none">gone</p><p>after</p>'
    )
    for gone in ("tmpl", "ns", "secret", "inner", "still", "gone"):
        _check(gone not in text, f"{gone!r} leaked: {text!r}")
    _check("seen" in text and "after" in text, text)

    text, _ = html_to_text("<h2>\n 3.1 Training\n</h2><p>body</p>")
    _check(
        any(
            line.startswith("## ") and "3.1 Training" in line
            for line in text.splitlines()
        ),
        repr(text),
    )

    text, warns = html_to_text("<p>before</p><nav>never closed<p>lost</p>")
    _check(warns and "nav" in warns[0] and "lost" not in text, (warns, text))
    print("selftest ok")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--selftest"]:
        return _selftest()
    if len(argv) != 2:
        print(__doc__)
        return 2
    with open(argv[0], encoding="utf-8", errors="replace") as fh:
        text, warnings = html_to_text(fh.read())
    with open(argv[1], "w", encoding="utf-8") as fh:
        fh.write(text)
    for w in warnings:
        print("WARNING:", w, file=sys.stderr)
    print(f"{len(text)} chars, {text.count(chr(10))} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
