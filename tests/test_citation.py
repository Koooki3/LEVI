"""CITATION.cff, docs/REFERENCES.md and docs/references.bib stay valid and in step with the release."""

import re
import tomllib
from pathlib import Path

import yaml  # PyYAML arrives with uvicorn[standard]; a missing module fails the test instead of skipping it

ROOT = Path(__file__).resolve().parents[1]


def cff():
    return yaml.safe_load((ROOT / "CITATION.cff").read_text(encoding="utf-8"))


def test_the_citation_file_has_the_fields_github_and_cffconvert_need():
    data = cff()
    assert data["cff-version"] == "1.2.0"
    for key in (
        "message",
        "title",
        "authors",
        "type",
        "version",
        "date-released",
        "license",
        "repository-code",
    ):
        assert data.get(key), key
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(data["date-released"]))
    assert all(
        {"name", "alias", "family-names", "given-names"} & set(a)
        for a in data["authors"]
    )


def test_the_citation_names_the_released_version_and_repository():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    data = cff()
    assert data["version"] == project["version"], (
        "bump CITATION.cff with the version (docs/RELEASING.md)"
    )
    assert data["license"] == project["license"]
    assert data["repository-code"] == project["urls"]["Repository"]


def test_every_reference_is_typed_titled_and_attributed():
    refs = cff()["references"]
    assert len(refs) >= 10
    for ref in refs:
        assert ref.get("type") and ref.get("title"), ref
        assert ref.get("authors"), ref["title"]
        assert any(ref.get(k) for k in ("url", "repository-code", "doi")), ref["title"]


def test_references_page_bibtex_and_cff_list_the_same_arxiv_papers():
    page = (ROOT / "docs/REFERENCES.md").read_text(encoding="utf-8")
    bib = (ROOT / "docs/references.bib").read_text(encoding="utf-8")
    cff_text = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    ids = set(re.findall(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", page))
    assert len(ids) >= 10
    for arxiv in ids:
        assert arxiv in bib, f"{arxiv} missing from references.bib"
        assert arxiv in cff_text, f"{arxiv} missing from CITATION.cff"


def test_bibtex_entries_have_unique_keys_and_balanced_braces():
    bib = (ROOT / "docs/references.bib").read_text(encoding="utf-8")
    keys = re.findall(r"^@\w+\{([^,]+),", bib, flags=re.MULTILINE)
    assert len(keys) == len(set(keys)) >= 15
    for entry in re.split(r"\n(?=@)", bib):
        if entry.startswith("@"):
            assert entry.count("{") == entry.count("}"), entry.splitlines()[0]


def test_the_bibtex_entry_for_levi_names_the_released_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    bib = (ROOT / "docs/references.bib").read_text(encoding="utf-8")
    entry = re.search(r"@software\{levi2026,.*?\n\}", bib, flags=re.DOTALL).group(0)
    assert f"version = {{{project['version']}}}" in entry, (
        "bump docs/references.bib with the version (docs/RELEASING.md)"
    )
