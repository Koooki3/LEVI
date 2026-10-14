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


def test_committed_figures_are_what_the_generator_makes(tmp_path):
    module = build_module(tmp_path)
    assert module.main() == 0
    made = sorted(p.name for p in tmp_path.glob("*.svg"))
    assert made and made == sorted(p.name for p in ATLAS.glob("*.svg"))
    stale = [n for n in made if (tmp_path / n).read_bytes() != (ATLAS / n).read_bytes()]
    assert not stale, f"run `python3 docs/atlas/build.py`: {stale}"


def test_every_figure_has_both_themes_a_claim_and_nothing_executable():
    names = {re.sub(r"-(light|dark)\.svg$", "", p.name) for p in ATLAS.glob("*.svg")}
    assert len(names) == 7
    for name in names:
        for theme in ("light", "dark"):
            path = ATLAS / f"{name}-{theme}.svg"
            assert path.is_file(), path
            text = path.read_text(encoding="utf8")
            root = ET.fromstring(text)  # well-formed
            assert root.attrib["role"] == "img"
            assert not re.search(r"<(script|foreignObject|style|image)\b", text)
            title = next(e for e in root.iter() if e.tag.endswith("title"))
            desc = next(e for e in root.iter() if e.tag.endswith("desc"))
            assert len(title.text) > 20 and len(desc.text) > 40


def test_the_pages_that_show_the_atlas_link_files_that_exist():
    for page in ("docs/ARCHITECTURE.md", "README.md", "README.zh-CN.md"):
        text = (ROOT / page).read_text(encoding="utf8")
        base = (ROOT / page).parent
        for target in re.findall(r"(?:srcset|src)=\"([^\"]+\.svg)\"", text):
            assert (base / target).is_file(), (page, target)
