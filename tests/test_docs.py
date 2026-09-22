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
