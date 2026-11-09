"""levi.decision stays a pure core: it imports neither levi.automatic nor levi.curation,
and neither of those imports it (design 4.1; U19)."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "levi"


def imported_modules(source: str, package: str) -> set[str]:
    """Absolute dotted names a module imports; ``from levi import x`` counts as levi.x."""
    out: set[str] = set()
    parts = package.split(".")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = parts[: len(parts) - node.level + 1]
                base = ".".join([*anchor, base] if base else anchor)
            out.add(base)
            out.update(f"{base}.{a.name}" for a in node.names)
    return out


def hits(source: str, package: str, forbidden: tuple[str, ...]) -> set[str]:
    return {
        m
        for m in imported_modules(source, package)
        if any(m == f or m.startswith(f + ".") for f in forbidden)
    }


def scan(folder: str, forbidden: tuple[str, ...]) -> dict[str, set[str]]:
    found = {}
    for path in sorted((ROOT / folder).rglob("*.py")):
        rel = path.relative_to(ROOT.parent).with_suffix("")
        package = ".".join(rel.parts[:-1])
        bad = hits(path.read_text(), package, forbidden)
        if bad:
            found[str(path.relative_to(ROOT.parent))] = bad
    return found


def test_the_scanner_sees_every_import_form():
    forbidden = ("levi.automatic", "levi.curation")
    for src in (
        "import levi.automatic",
        "import levi.automatic.loop as x",
        "from levi.automatic import x",
        "from levi import automatic",
        "from .. import curation",
        "from ..automatic import x",
    ):
        assert hits(src, "levi.decision", forbidden), src
    assert not hits(
        "from .contracts import Route\nimport math", "levi.decision", forbidden
    )


def test_decision_does_not_import_automatic_or_curation():
    assert scan("decision", ("levi.automatic", "levi.curation")) == {}


def test_automatic_and_curation_do_not_import_decision():
    # levi/curation does not exist yet; the scan then has nothing to read there.
    assert scan("automatic", ("levi.decision",)) == {}
    if (ROOT / "curation").is_dir():
        assert scan("curation", ("levi.decision",)) == {}
