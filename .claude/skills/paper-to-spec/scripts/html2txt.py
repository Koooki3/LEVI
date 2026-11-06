#!/usr/bin/env python3
"""Plain text from an HTML paper (arXiv HTML / LaTeXML). Standard library only.

    python3 -I html2txt.py <in.html> <out.txt>     # headings (h1-h4) become "## " lines
    python3 -I html2txt.py --selftest

Scripts, styles, navigation, headers, footers, svg and buttons are dropped. The input is untrusted
data: it is only parsed, never executed. Run it from a directory other than the one holding the
downloaded file, with `-I`, so that nothing next to the input can shadow a standard-library module.
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser

SKIP = {"script", "style", "nav", "header", "footer", "svg", "button"}
BLOCK = {"p", "div", "section", "h1", "h2", "h3", "h4", "h5", "li", "tr", "table", "figcaption", "br", "pre", "blockquote"}
HEADING = {"h1", "h2", "h3", "h4"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
        if tag in BLOCK:
            self.out.append("\n")
        if tag in HEADING and not self.skip:
            self.out.append("\n## ")

    def handle_endtag(self, tag):
        if tag in SKIP and self.skip:
            self.skip -= 1
        if tag in BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    text = "".join(parser.out)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip() + "\n"


def _selftest() -> int:
    html = (
        "<html><head><style>p{color:red}</style><script>alert(1)</script></head><body>"
        "<nav>menu</nav><h2>2 Method</h2><p>First  sentence.</p><div>Second<br>line</div>"
        "<button>click</button><footer>foot</footer></body></html>"
    )
    text = html_to_text(html)
    assert "## 2 Method" in text, text
    assert "First sentence." in text, text
    assert "alert" not in text and "color:red" not in text and "menu" not in text and "click" not in text and "foot" not in text, text
    assert "Second" in text and "line" in text, text
    assert not text.startswith("\n") and text.endswith("\n") and not text.endswith("\n\n"), repr(text)
    print("selftest ok")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--selftest"]:
        return _selftest()
    if len(argv) != 2:
        print(__doc__)
        return 2
    with open(argv[0], encoding="utf-8", errors="replace") as fh:
        text = html_to_text(fh.read())
    with open(argv[1], "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"{len(text)} chars, {text.count(chr(10))} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
