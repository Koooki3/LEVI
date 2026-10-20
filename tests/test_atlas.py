"""The architecture atlas: every figure exists in both themes, is self-contained SVG,
says its claim, and the committed files are what the generator makes."""

import importlib.util
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

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


OPACITY = r"0?\.[2-9]\d*|1"  # never below .2: motion may dim, not hide
KEYFRAME_VALUES = {
    "stroke-dashoffset": r"-?\d{1,3}",
    "transform": r"translateX\((?:0|-?\d{1,4}px)\)",
    "opacity": OPACITY,
}
KEYFRAME_PROPS = set(KEYFRAME_VALUES)
TIMING = {"linear", "ease-in-out", "infinite"}
# Each class declaration value has one exact shape: no "!important" (it would beat the reduced-motion rule),
# no comma lists, calc(), exponents or upper-case units, and only TIMING words after the duration.
CLASS_VALUES = {
    "animation": r"[a-z][\w-]* (?P<s>\d+(?:\.\d+)?)s(?: (?:" + "|".join(TIMING) + "))*",
    "animation-delay": r"-?\d+(?:\.\d+)?s",
    "opacity": OPACITY,
}
MAX_STOPS = 3  # keyframe selectors (from, to, N%) per animation; the generator uses at most 3: no strobe


def declarations(name, body):
    """[(prop, value)] of "a:b;c:d"; a property or value outside the plain CSS alphabet fails."""
    out = []
    for decl in filter(None, body.split(";")):
        prop, sep, value = decl.partition(":")
        assert sep and re.fullmatch(r"[a-z-]+", prop), f"{name}: declaration {decl!r}"
        assert re.fullmatch(r"[\w.%() -]+", value), f"{name}: value {value!r}"
        out.append((prop, value))
    return out


def check_motion_css(name, css):
    """Parse CSS made of @keyframes blocks and .class{...} rules, strictly: keyframes have at most MAX_STOPS
    blocks setting only KEYFRAME_PROPS, class rules set only the CLASS_VALUES properties in their exact shape,
    and every animation lasts at least 3 s."""
    keyframes = r"@keyframes [a-z][\w-]*\{((?:[^{}]|\{[^{}]*\})*)\}"
    for body in re.findall(keyframes, css):
        stop = r"(?:from|to|\d+%(?:,\d+%)*)\{([^{}]*)\}"
        assert re.fullmatch(f"(?:{stop})+", body), f"{name}: keyframes {body!r}"
        stops = re.findall(r"from|to|\d+%", re.sub(r"\{[^{}]*\}", "", body))
        assert len(stops) <= MAX_STOPS, f"{name}: {len(stops)} keyframe stops"
        for block in re.findall(stop, body):
            for prop, value in declarations(name, block):
                assert prop in KEYFRAME_VALUES, f"{name}: keyframes set {prop}"
                assert re.fullmatch(KEYFRAME_VALUES[prop], value), (
                    f"{name}: keyframes {prop} {value!r}"
                )
    rules = re.sub(keyframes, "", css)
    pattern = r"\.[a-z][\w-]*\{([^{}]*)\}"
    assert re.fullmatch(f"(?:{pattern})*", rules), f"{name}: unexpected CSS {rules!r}"
    checked = 0
    for body in re.findall(pattern, rules):
        for prop, value in declarations(name, body):
            assert prop in CLASS_VALUES, f"{name}: class rule sets {prop}"
            shape = re.fullmatch(CLASS_VALUES[prop], value)
            assert shape, f"{name}: {prop} {value!r}"
            if prop == "animation":
                assert float(shape["s"]) >= 3, f"{name}: period {shape['s']}s"
                checked += 1
    assert checked == len(re.findall(r"animation\s*:", css, re.IGNORECASE)), (
        f"{name}: an animation was not checked"
    )


def check_executable_free(name, text, module):
    """Motion is CSS only: a fixed <style>, no script, SMIL, links, transitions or fast loops."""
    assert not re.search(r"<(script|foreignObject|image)\b", text), name
    assert not re.search(r"<(animate\w*|set)\b", text), f"{name}: SMIL"
    assert not re.search(r"\son[a-z]+\s*=|href\s*=|@import", text), name
    assert not re.search(r"url\((?!#)", text), f"{name}: url( must point at #id"
    assert len(re.findall(r"<(?:\w+:)?style\b", text)) == 1, f"{name}: one <style>"
    assert not re.search(r"\sstyle\s*=", text), f"{name}: no style= attribute"
    styles = re.findall(r"<style>(.*?)</style>", text, re.DOTALL)
    assert len(styles) == 1, name
    css = styles[0]
    assert "/*" not in css, f"{name}: no comments in <style>"
    assert not re.search(r"transition|animation-duration|\d\s*ms\b", css), name
    # exactly the generator's blocks, in order, then the reduced-motion rule
    assert len(re.findall(REDUCED, css)) == 1, f"{name}: one reduced-motion rule"
    reduced = re.search(REDUCED, css)
    assert reduced and css.endswith(reduced.group(0)), f"{name}: reduced-motion rule"
    assert reduced.group(0) == module.Fig.CSS_REDUCED
    rest = css[: reduced.start()]
    expected = "".join(v for k, v in module.Fig.CSS_PARTS.items() if v in rest)
    assert rest == expected, f"{name}: <style> differs from the generator's blocks"
    # inside the allowed blocks: animated properties and periods
    check_motion_css(name, rest)
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
    assert len(names) == 2 * len(module.FIGURES)  # every figure in English and Chinese
    assert {n for n in names if not n.endswith("-zh")} == {
        n[:-3] for n in names if n.endswith("-zh")
    }
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
            short, long = (
                (8, 20) if name.endswith("-zh") else (20, 40)
            )  # Chinese says it in fewer characters
            assert len(title.text) > short and len(desc.text) > long


def test_the_pages_that_show_the_atlas_link_files_that_exist():
    for page in ("docs/ARCHITECTURE.md", "README.md", "README.zh-CN.md"):
        text = (ROOT / page).read_text(encoding="utf8")
        base = (ROOT / page).parent
        for target in re.findall(r"(?:srcset|src)=\"([^\"]+\.svg)\"", text):
            assert (base / target).is_file(), (page, target)


def test_every_motion_block_of_the_generator_is_checked_even_if_no_figure_uses_it():
    module = build_module(ATLAS)
    for key, part in module.Fig.CSS_PARTS.items():
        check_motion_css(key, part)


@pytest.mark.parametrize(
    "css",
    [
        ".x{animation:1s x linear infinite}",
        ".x{animation: x 900ms linear infinite}",
        ".x{animation:x linear infinite}",
        ".x{display:none}",
        ".x{opacity:.5;width:3px}",
        "@keyframes k{to{width:3px}}",
        ".x{animation:x 3s linear infinite}.y{animation:y 2.5s linear infinite}",
        ".x{animation:x 3s}p{color:red}",
        ".x{animation:x 0.5s,y 3s}",
        ".x{animation:x 1S 3s linear infinite}",
        ".x{animation:x 1e0s 3s linear infinite}",
        ".x{animation:x calc(1s) 3s linear infinite}",
        ".x{animation:x 900ms 3s linear infinite}",
        ".x{animation:x 3s linear infinite !important}",
        ".x{opacity:.6 !important}",
        "@keyframes k{to{WIDTH:3px}}",
        "@keyframes k{to{width/**/:3px}}",
        "@keyframes k{0%{opacity:0}1%{opacity:1}2%{opacity:0}3%{opacity:1}}",
        ".x{animation:k 3s steps(1) infinite}",
        "@keyframes k{0%,2%,4%,6%{opacity:.2}1%,3%,5%{opacity:1}}",
        "@keyframes k{0%,50%,100%{transform:translateX(0)}25%,75%{transform:translateX(900px)}}",
        "@keyframes k{to{transform:scale(0)}}",
        "@keyframes k{to{transform:translateX(-99999px)}}",
        "@keyframes k{to{stroke-dashoffset:-99999}}",
        "@keyframes k{to{opacity:0}}",
        ".x{opacity:.0}",
        ".x{opacity:.1}",
    ],
)
def test_the_motion_check_rejects(css):
    with pytest.raises(AssertionError):
        check_motion_css("mutant", css)


def test_one_reduced_motion_rule_only():
    module = build_module(ATLAS)
    text = (ATLAS / "05-live-gpu-light.svg").read_text(encoding="utf8")
    doubled = text.replace(
        module.Fig.CSS_REDUCED,
        module.Fig.CSS_REDUCED + "*{animation:z 1s infinite}" + module.Fig.CSS_REDUCED,
    )
    with pytest.raises(AssertionError):
        check_executable_free("doubled", doubled, module)


@pytest.mark.parametrize(
    "extra",
    [
        '<style media="all">*{animation:z .1s infinite}</style>',
        '<svg:style xmlns:svg="http://www.w3.org/2000/svg">*{opacity:0}</svg:style>',
        '<g style="animation:k .1s infinite !important;opacity:0"/>',
    ],
)
def test_css_outside_the_one_style_element_is_refused(extra):
    module = build_module(ATLAS)
    text = (ATLAS / "05-live-gpu-light.svg").read_text(encoding="utf8")
    with pytest.raises(AssertionError):
        check_executable_free("extra", text.replace("</svg>", extra + "</svg>"), module)


def test_chinese_variants_translate_every_drawn_string():
    """The zh pass draws only table entries; no English sentence is left in a zh figure."""
    module = build_module(ATLAS)
    zh = {p.name for p in ATLAS.glob("*-zh-*.svg")}
    assert len(zh) == 4 * len(module.FIGURES) // 2  # two themes per figure
    for name in zh:
        text = (ATLAS / name).read_text(encoding="utf8")
        drawn = re.findall(r"<text [^>]*>([^<]*)</text>", text)
        assert drawn, name
        assert not [
            s for s in drawn if re.search(r"[A-Za-z]{3,} [a-z]{3,} [a-z]{3,}", s)
        ], name
    assert module.tr(
        "Data goes in, models propose, a person decides, curated training data comes out."
    ) == (
        "Data goes in, models propose, a person decides, curated training data comes out."
    )  # the English pass is the default


def test_chinese_status_badges_use_the_readme_words():
    """Chinese badges use only main / 集成分支 / 计划中, never the English
    "branch" or "planned" (the words came from README.zh-CN.md). When nothing
    is on the integration branch no figure draws 集成分支, so only main and
    计划中 must appear."""
    badges = set()
    for path in ATLAS.glob("*-zh-*.svg"):
        text = path.read_text(encoding="utf8")
        # a badge is the 16-unit-high pill Fig.tag draws, followed by its label
        badges |= set(
            re.findall(
                r'height="16" rx="8"[^>]*/><text [^>]*font-weight="700"[^>]*>([^<]*)</text>',
                text,
            )
        )
    assert {"main", "计划中"} <= badges <= {"main", "集成分支", "计划中"}, badges


# README figures still waiting for a large-text version (PROMO-ATLAS-LEGIBILITY).
README_SMALL_TEXT_PENDING = {"07-evidence"}


def test_readme_figures_are_large_enough_to_read():
    """GitHub shows a README figure about 830-880 px wide, so a 1200-wide canvas
    shrinks to ~0.7: body text needs 18 px there (~12.6 px on screen) and no
    note may go below 15 px. The full figures stay in docs/ARCHITECTURE.md."""
    for page in ("README.md", "README.zh-CN.md"):
        text = (ROOT / page).read_text(encoding="utf8")
        for target in sorted(
            set(re.findall(r"(?:srcset|src)=\"([^\"]+\.svg)\"", text))
        ):
            figure = re.sub(r"(-zh)?-(light|dark)\.svg$", "", Path(target).name)
            if figure in README_SMALL_TEXT_PENDING:
                continue
            svg = (ROOT / target).read_text(encoding="utf8")
            sizes = [
                float(s) for s in re.findall(r"<text [^>]*font-size=\"([\d.]+)\"", svg)
            ]
            assert sizes, target
            assert min(sizes) >= 15, (page, target, min(sizes))
            large = sum(s >= 18 for s in sizes)
            assert large >= 0.6 * len(sizes), (page, target, large, len(sizes))
