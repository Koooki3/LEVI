"""The architecture atlas: every figure exists in both themes, is self-contained SVG,
says its claim, and the committed files are what the generator makes."""

import importlib.util
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "docs" / "assets" / "atlas"


def build_module(out: Path):
    spec = importlib.util.spec_from_file_location(
        "atlas_build", ROOT / "docs/atlas/build.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.OUT = out
    return module


REDUCED = r"@media \(prefers-reduced-motion: reduce\) \{ \* \{ animation: none !important; \} \}"


def check_executable_free(name, text, module):
    """Motion is CSS only: a fixed <style>, no script, SMIL, links, transitions or fast loops."""
    assert not re.search(r"<(script|foreignObject|image)\b", text), name
    assert not re.search(r"<(animate\w*|set)\b", text), f"{name}: SMIL"
    assert not re.search(r"\son[a-z]+\s*=|href\s*=|@import", text), name
    assert not re.search(r"url\((?!#)", text), f"{name}: url( must point at #id"
    styles = re.findall(r"<style>(.*?)</style>", text, re.DOTALL)
    assert len(styles) == 1, name
    css = styles[0]
    assert "/*" not in css, f"{name}: no comments in <style>"
    assert not re.search(r"transition|animation-duration|\d\s*ms\b", css), name
    # exactly the generator's blocks, in order, then the reduced-motion rule
    reduced = re.search(REDUCED, css)
    assert reduced and css.endswith(reduced.group(0)), f"{name}: reduced-motion rule"
    assert reduced.group(0) == module.Fig.CSS_REDUCED
    rest = css[: reduced.start()]
    expected = "".join(v for k, v in module.Fig.CSS_PARTS.items() if v in rest)
    assert rest == expected, f"{name}: <style> differs from the generator's blocks"
    # inside the allowed blocks: animated properties and periods
    for body in re.findall(r"@keyframes \w+\{((?:[^{}]|\{[^{}]*\})*)\}", rest):
        assert set(re.findall(r"([a-z-]+)\s*:", body)) <= {
            "stroke-dashoffset",
            "transform",
            "opacity",
        }, name
    for period in re.findall(r"animation:\s*\w+\s+([\d.]+)s", css):
        assert float(period) >= 3, f"{name}: period {period}s"
    assert len(re.findall(r'filter="url', text)) <= 10, name


def test_committed_figures_are_what_the_generator_makes(tmp_path):
    module = build_module(tmp_path)
    assert module.main() == 0
    made = sorted(p.name for p in tmp_path.glob("*.svg"))
    assert made and made == sorted(p.name for p in ATLAS.glob("*.svg"))
    stale = [n for n in made if (tmp_path / n).read_bytes() != (ATLAS / n).read_bytes()]
    assert not stale, f"run `python3 docs/atlas/build.py`: {stale}"


def test_every_figure_has_both_themes_a_claim_and_nothing_executable():
    module = build_module(ATLAS)
    names = {re.sub(r"-(light|dark)\.svg$", "", p.name) for p in ATLAS.glob("*.svg")}
    assert len(names) == 8
    for name in names:
        for theme in ("light", "dark"):
            path = ATLAS / f"{name}-{theme}.svg"
            assert path.is_file(), path
            text = path.read_text(encoding="utf8")
            root = ET.fromstring(text)  # well-formed
            assert root.attrib["role"] == "img"
            check_executable_free(path.name, text, module)
            title = next(e for e in root.iter() if e.tag.endswith("title"))
            desc = next(e for e in root.iter() if e.tag.endswith("desc"))
            assert len(title.text) > 20 and len(desc.text) > 40


def test_the_pages_that_show_the_atlas_link_files_that_exist():
    for page in ("docs/ARCHITECTURE.md", "README.md", "README.zh-CN.md"):
        text = (ROOT / page).read_text(encoding="utf8")
        base = (ROOT / page).parent
        for target in re.findall(r"(?:srcset|src)=\"([^\"]+\.svg)\"", text):
            assert (base / target).is_file(), (page, target)
