"""The documentation keeps up with the code (levi/docs.py)."""

from levi import docs


def test_the_docs_are_in_step_with_the_code():
    # Fix what this lists, or run `uv run levi docs sync` for generated parts.
    assert docs.check() == []


def test_a_generated_section_is_rewritten_in_place():
    text = "a\n<!-- levi:generated x -->\nold\n<!-- /levi:generated x -->\nb"
    assert docs._render(text, "x", "new\n") == (
        "a\n<!-- levi:generated x -->\nnew\n<!-- /levi:generated x -->\nb"
    )


def test_one_section_can_be_generated_into_several_files():
    found = docs.generated()
    names = [(name, file) for name, file, _ in found]
    # The single-file sections keep their file (regression).
    assert ("capabilities", "docs/API.md") in names
    assert ("knowledge", "docs/KNOWLEDGE.md") in names
    assert [f for n, f in names if n == "capabilities"] == ["docs/API.md"]
    assert [f for n, f in names if n == "knowledge"] == ["docs/KNOWLEDGE.md"]
    assert [f for n, f in names if n == "aeri-reset-modes"] == [
        "docs/AUTOMATIC_PIPELINE.md",
        "docs/AUTOMATIC_PIPELINE.zh-CN.md",
    ]
    assert len(names) == len(set(names))


def test_sync_writes_every_file_of_a_section_and_is_idempotent(tmp_path, monkeypatch):
    for file in ("a.md", "b.md", "c.md"):
        (tmp_path / file).write_text(
            f"# {file}\n<!-- levi:generated x -->\nold\n<!-- /levi:generated x -->\n"
            "<!-- levi:generated y -->\nold\n<!-- /levi:generated y -->\n"
        )
    monkeypatch.setattr(docs, "PROJECT", tmp_path)
    monkeypatch.setattr(
        docs,
        "GENERATED",
        {
            "x": ("a.md", lambda: "one\n"),
            "y": [("b.md", lambda: "en\n"), ("c.md", lambda: "zh\n")],
        },
    )
    assert docs.sync() == ["a.md", "b.md", "c.md"]
    assert "x -->\none\n" in (tmp_path / "a.md").read_text()
    assert "y -->\nold\n" in (tmp_path / "a.md").read_text()  # not a's section
    assert "y -->\nen\n" in (tmp_path / "b.md").read_text()
    assert "y -->\nzh\n" in (tmp_path / "c.md").read_text()
    before = {f: (tmp_path / f).read_text() for f in ("a.md", "b.md", "c.md")}
    assert docs.sync() == []  # a second sync changes nothing
    assert before == {f: (tmp_path / f).read_text() for f in before}
    (tmp_path / "c.md").write_text("# c.md\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/AGENTS.md").write_text("")
    problems = [p for p in docs.check() if ".md" in p and "section" in p]
    assert problems == ["c.md has no generated section `y`"]


def test_anchors_follow_github_heading_slugs():
    text = (
        "# Codebase map\n## 3D URDF viewer\n### `levi/pool/` (read-only)\n"
        "## Same\n## Same\n```\n# not a heading\n```\n## 被动 State, v3.1\n"
    )
    assert docs.anchors(text) == {
        "codebase-map",
        "3d-urdf-viewer",
        "levipool-read-only",
        "same",
        "same-1",
        "被动-state-v31",
    }


def _anchor_problems(tmp_path, monkeypatch, heading):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/AGENTS.md").write_text("")
    (tmp_path / "docs/MAP.md").write_text(f"# Map\n\n## {heading}\n")
    (tmp_path / "README.md").write_text(
        "[a](docs/MAP.md#frontend-data-layer) [b](#top) [c](https://x.y/z#q)\n# Top\n"
    )
    monkeypatch.setattr(docs, "PROJECT", tmp_path)
    return [p for p in docs.check() if "links to" in p]


def test_a_link_anchor_that_matches_a_heading_passes(tmp_path, monkeypatch):
    assert _anchor_problems(tmp_path, monkeypatch, "Frontend data layer") == []


def test_renaming_a_heading_breaks_the_links_to_it(tmp_path, monkeypatch):
    # Mutation check: the same tree with one heading renamed must go red.
    problems = _anchor_problems(tmp_path, monkeypatch, "Frontend data")
    assert problems == [
        "README.md links to `docs/MAP.md#frontend-data-layer`, which is not a heading there"
    ]
