"""The structured third-party record of levi.events (third_party.json)."""

import copy
from pathlib import Path

from levi.events import third_party

ROOT = Path(__file__).resolve().parents[1]


def test_the_registry_keeps_its_rules():
    assert third_party.problems(third_party.load(), ROOT) == []


def test_ruptures_is_an_idea_not_a_dependency():
    registry = third_party.load()
    ruptures = next(c for c in registry["components"] if c["key"] == "ruptures")
    assert ruptures["use"] == "adopt-idea"
    assert not ruptures["shipped"] and not ruptures["code_copied"]
    assert ruptures["citation"]["arxiv"] == "1801.00718"
    assert "ruptures" not in (ROOT / "pyproject.toml").read_text()


def _broken(edit):
    registry = copy.deepcopy(third_party.load())
    edit(registry)
    return third_party.problems(registry, ROOT)


def test_restricted_licences_allow_only_ideas():
    def nc(r):
        r["components"].append(
            {
                "key": "x",
                "name": "x",
                "kind": "repo",
                "use": "adapt-code",
                "source": "https://example.invalid",
                "version": "1",
                "licenses": {"code": "CC BY-NC 4.0", "weights": "n/a", "data": "n/a"},
                "shipped": False,
                "code_copied": True,
                "redistributable": "no",
                "where": [],
            }
        )

    assert any("allows only adopt-idea" in p for p in _broken(nc))


def test_unverified_citations_dependencies_and_paths_are_caught():
    def unverified(r):
        r["components"][0]["reference_status"] = "UNVERIFIED"

    def undeclared(r):
        r["components"][1]["key"] = "scipy"

    def missing_file(r):
        r["components"][0]["where"] = ["levi/events/nowhere.py"]

    def missing_field(r):
        del r["components"][0]["licenses"]

    assert any("VERIFIED" in p for p in _broken(unverified))
    assert any("pyproject" in p for p in _broken(undeclared))
    assert any("does not exist" in p for p in _broken(missing_file))
    assert any("missing licenses" in p for p in _broken(missing_field))


def test_an_unregistered_arxiv_citation_is_caught(tmp_path):
    root = tmp_path
    (root / "levi" / "events").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\ndependencies = ["numpy>=2"]\n')
    (root / "levi" / "events" / "m.py").write_text('"""See arXiv:2401.00001."""\n')
    found = third_party.problems(
        {"schema_version": third_party.SCHEMA, "components": []}, root
    )
    assert found == [
        "levi/events/m.py cites arXiv:2401.00001, which no registered component carries"
    ]
