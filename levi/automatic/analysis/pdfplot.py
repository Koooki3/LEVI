"""A minimal single-page vector PDF writer for a ``FigureSpec`` (stdlib only).

What it writes: one page, an uncompressed content stream of lines, rectangles,
polygons and Bezier circles, and text in the standard Helvetica and
Helvetica-Bold fonts (PDF base-14: nothing is embedded, every reader has
them), encoded as WinAnsi. Text is real text: ``pdftotext`` and a reader's
search find it. There is no clock, no ``/ID`` and no random value, so the
same spec gives the same bytes.

Latin only: the base-14 fonts have no CJK glyphs. A string that Windows-1252
cannot encode falls back to its English form (``Text.get(latin_only=True)``),
and a character that still cannot be encoded becomes ``?``. Figures that need
Chinese labels are written as SVG (or shown in the web page).

``verify_pdf`` parses the file again with its own small reader (header, xref
offsets, trailer, page tree, stream lengths, fonts) and extracts the text,
so a test can check the structure without a third-party reader.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .figspec import (
    Circle,
    FigureSpec,
    Label,
    Line,
    Poly,
    Rect,
    Scene,
    coord,
    layout,
    rgb_of,
    text_width,
)

KAPPA = 0.5522847498  # Bezier circle constant
PRODUCER = "LEVI figspec"


def _col(hex_colour: str) -> str:
    return " ".join(_n(c / 255.0) for c in rgb_of(hex_colour))


def _n(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _pdf_string(s: str) -> bytes:
    """A literal string in WinAnsi (Windows-1252) with PDF escapes."""
    raw = s.encode("cp1252", "replace")
    out = bytearray(b"(")
    for b in raw:
        if b in b"\\()":
            out += b"\\" + bytes([b])
        elif b < 32 or b > 126:
            out += b"\\%03o" % b
        else:
            out.append(b)
    out += b")"
    return bytes(out)


def _hex_utf16(s: str) -> bytes:
    return b"<FEFF" + s.encode("utf-16-be").hex().upper().encode("ascii") + b">"


class _Page:
    def __init__(self, height: float):
        self.h = height
        self.ops: list[str] = []

    def y(self, v: float) -> float:
        return self.h - v

    def stroke_style(self, color: str, width: float, dash: tuple[float, ...]) -> str:
        d = "[" + " ".join(coord(x) for x in dash) + "] 0 d" if dash else "[] 0 d"
        return f"{_col(color)} RG {coord(width)} w {d} 0 j 0 J"

    def add(self, it) -> None:
        o = self.ops
        if isinstance(it, Line):
            o.append(
                f"q {self.stroke_style(it.stroke, it.width, it.dash)} "
                f"{coord(it.x1)} {coord(self.y(it.y1))} m {coord(it.x2)} {coord(self.y(it.y2))} l S Q"
            )
        elif isinstance(it, Rect):
            parts = ["q"]
            if it.fill:
                parts.append(f"{_col(it.fill)} rg")
            if it.stroke:
                parts.append(self.stroke_style(it.stroke, it.stroke_width, ()))
            op = "B" if it.fill and it.stroke else "f" if it.fill else "S"
            parts.append(
                f"{coord(it.x)} {coord(self.y(it.y + it.h))} {coord(it.w)} {coord(it.h)} re {op} Q"
            )
            o.append(" ".join(parts))
        elif isinstance(it, Poly):
            pts = " ".join(
                f"{coord(x)} {coord(self.y(y))} {'m' if i == 0 else 'l'}"
                for i, (x, y) in enumerate(it.points)
            )
            parts = ["q"]
            if it.fill:
                parts.append(f"{_col(it.fill)} rg")
            if it.stroke:
                parts.append(self.stroke_style(it.stroke, it.width, it.dash))
            if it.closed:
                pts += " h"
            op = ("B" if it.stroke else "f") if it.fill else "S"
            if it.fill and not it.closed:
                pts += " h"
            parts.append(f"{pts} {op} Q")
            o.append(" ".join(parts))
        elif isinstance(it, Circle):
            cx, cy, r = it.cx, self.y(it.cy), it.r
            k = KAPPA * r
            path = (
                f"{coord(cx + r)} {coord(cy)} m "
                f"{coord(cx + r)} {coord(cy + k)} {coord(cx + k)} {coord(cy + r)} {coord(cx)} {coord(cy + r)} c "
                f"{coord(cx - k)} {coord(cy + r)} {coord(cx - r)} {coord(cy + k)} {coord(cx - r)} {coord(cy)} c "
                f"{coord(cx - r)} {coord(cy - k)} {coord(cx - k)} {coord(cy - r)} {coord(cx)} {coord(cy - r)} c "
                f"{coord(cx + k)} {coord(cy - r)} {coord(cx + r)} {coord(cy - k)} {coord(cx + r)} {coord(cy)} c h"
            )
            parts = ["q"]
            if it.fill:
                parts.append(f"{_col(it.fill)} rg")
            if it.stroke:
                parts.append(self.stroke_style(it.stroke, it.stroke_width, ()))
            op = "B" if it.fill and it.stroke else "f" if it.fill else "S"
            o.append(" ".join(parts) + f" {path} {op} Q")
        elif isinstance(it, Label):
            self.text(it)
        else:
            raise TypeError(f"unknown scene item {type(it).__name__}")

    def text(self, t: Label) -> None:
        if not t.s:
            return
        w = text_width(t.s, t.size, t.bold)
        shift = {"start": 0.0, "middle": w / 2, "end": w}[t.anchor]
        font = "F2" if t.bold else "F1"
        if t.rotate == -90:  # counter-clockwise: the text runs upward
            tm = f"0 1 -1 0 {coord(t.x)} {coord(self.y(t.y) - shift)} Tm"
        else:
            tm = f"1 0 0 1 {coord(t.x - shift)} {coord(self.y(t.y))} Tm"
        self.ops.append(
            f"q {_col(t.fill)} rg BT /{font} {coord(t.size)} Tf {tm} "
            + _pdf_string(t.s).decode("latin-1")
            + " Tj ET Q"
        )

    def stream(self) -> bytes:
        return ("\n".join(self.ops) + "\n").encode("latin-1")


def scene_to_pdf(scene: Scene) -> bytes:
    page = _Page(scene.height)
    page.ops.append(f"q 1 1 1 rg 0 0 {coord(scene.width)} {coord(scene.height)} re f Q")
    for it in scene.items:
        page.add(it)
    content = page.stream()
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R /Lang ("
        + scene.lang.encode("ascii")
        + b") >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {coord(scene.width)} {coord(scene.height)}] "
            "/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>"
        ).encode("ascii"),
        b"<< /Length "
        + str(len(content)).encode("ascii")
        + b" >>\nstream\n"
        + content
        + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Title "
        + _hex_utf16(scene.title)
        + b" /Subject "
        + _hex_utf16(scene.desc)
        + b" /Producer ("
        + PRODUCER.encode("ascii")
        + b") >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 7 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def render_pdf(
    spec: FigureSpec, lang: str | None = None, width: float = 640.0
) -> bytes:
    """The figure as a one-page PDF (bytes). Latin text only (see module doc)."""
    return scene_to_pdf(layout(spec, lang=lang, latin_only=True, width=width))


def write_pdf(spec: FigureSpec, path, lang: str | None = None) -> None:
    data = render_pdf(spec, lang=lang)
    tmp = f"{path}.partial"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# A small reader that checks the file it is given
# --------------------------------------------------------------------------


class PdfError(ValueError):
    """The bytes are not a PDF this module's reader can follow."""


@dataclass(frozen=True)
class PdfReport:
    objects: int
    page_width: float
    page_height: float
    fonts: tuple[str, ...]
    text: tuple[str, ...]  # every string shown with Tj, in drawing order


_ESC = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}


def _decode_literal(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == 0x5C and i + 1 < len(raw):  # backslash
            n = raw[i + 1]
            if 0x30 <= n <= 0x37:
                j = i + 1
                digits = b""
                while j < len(raw) and len(digits) < 3 and 0x30 <= raw[j] <= 0x37:
                    digits += bytes([raw[j]])
                    j += 1
                out.append(int(digits, 8))
                i = j
                continue
            out.append(_ESC.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return out.decode("cp1252", "replace")


def _fail(msg: str):
    raise PdfError(msg)


def verify_pdf(data: bytes) -> PdfReport:
    """Check header, trailer, cross-reference offsets, page tree, stream
    length, fonts and the end marker; return the page size and the text."""
    if not data.startswith(b"%PDF-1."):
        _fail("missing %PDF-1.x header")
    if not data.rstrip().endswith(b"%%EOF"):
        _fail("missing %%EOF")
    m = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", data)
    if not m:
        _fail("missing startxref")
    xref_at = int(m.group(1))
    if data[xref_at : xref_at + 4] != b"xref":
        _fail("startxref does not point at the xref table")
    head = re.match(rb"xref\s+0\s+(\d+)\s*\n", data[xref_at:])
    if not head:
        _fail("bad xref header")
    count = int(head.group(1))
    pos = xref_at + head.end()
    entries = []
    for i in range(count):
        e = data[pos + 20 * i : pos + 20 * (i + 1)]
        if len(e) != 20 or not re.fullmatch(
            rb"\d{10} \d{5} [nf] \r?\n|\d{10} \d{5} [nf]\s\s", e
        ):
            _fail(f"xref entry {i} is not 20 bytes")
        entries.append((int(e[:10]), e[17:18]))
    if entries[0] != (0, b"f"):
        _fail("xref entry 0 must be the free-list head")
    trailer = re.search(rb"trailer\s*<<(.*?)>>\s*startxref", data[xref_at:], re.DOTALL)
    if not trailer:
        _fail("missing trailer")
    td = trailer.group(1)
    size = re.search(rb"/Size\s+(\d+)", td)
    root = re.search(rb"/Root\s+(\d+)\s+0\s+R", td)
    if not size or int(size.group(1)) != count:
        _fail("trailer /Size does not match the xref table")
    if not root:
        _fail("trailer has no /Root")

    bodies: dict[int, bytes] = {}
    for i in range(1, count):
        off, kind = entries[i]
        if kind != b"n":
            _fail(f"object {i} is free")
        tag = f"{i} 0 obj\n".encode("ascii")
        if data[off : off + len(tag)] != tag:
            _fail(f"xref offset of object {i} does not point at '{i} 0 obj'")
        end = data.find(b"\nendobj", off)
        if end < 0:
            _fail(f"object {i} has no endobj")
        bodies[i] = data[off + len(tag) : end]

    cat = bodies[int(root.group(1))]
    if b"/Type /Catalog" not in cat:
        _fail("/Root is not a catalog")
    pages_ref = re.search(rb"/Pages\s+(\d+)\s+0\s+R", cat)
    if not pages_ref:
        _fail("catalog has no /Pages")
    pages = bodies[int(pages_ref.group(1))]
    if b"/Type /Pages" not in pages or not re.search(rb"/Count\s+1\b", pages):
        _fail("page tree must hold exactly one page")
    kid = re.search(rb"/Kids\s*\[\s*(\d+)\s+0\s+R\s*\]", pages)
    if not kid:
        _fail("page tree has no kid")
    page = bodies[int(kid.group(1))]
    if b"/Type /Page" not in page:
        _fail("kid is not a page")
    box = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", page)
    if not box:
        _fail("page has no MediaBox")
    contents = re.search(rb"/Contents\s+(\d+)\s+0\s+R", page)
    if not contents:
        _fail("page has no /Contents")
    fonts = []
    for ref in re.findall(rb"/F\d+\s+(\d+)\s+0\s+R", page):
        fb = bodies[int(ref)]
        name = re.search(rb"/BaseFont\s*/([\w-]+)", fb)
        if not name or b"/Type /Font" not in fb:
            _fail(f"font object {int(ref)} is not a font")
        fonts.append(name.group(1).decode("ascii"))
    if not fonts:
        _fail("page declares no font")

    sb = bodies[int(contents.group(1))]
    length = re.search(rb"/Length\s+(\d+)", sb)
    s0 = sb.find(b"stream\n")
    if not length or s0 < 0:
        _fail("content object is not a stream")
    stream = sb[s0 + 7 : s0 + 7 + int(length.group(1))]
    if sb[s0 + 7 + int(length.group(1)) :] != b"endstream":
        _fail("stream /Length does not match the data")
    text = []
    for lit in re.finditer(rb"\(((?:\\.|[^\\()])*)\)\s*Tj", stream):
        text.append(_decode_literal(lit.group(1)))
    return PdfReport(
        objects=count - 1,
        page_width=float(box.group(1)),
        page_height=float(box.group(2)),
        fonts=tuple(fonts),
        text=tuple(text),
    )
