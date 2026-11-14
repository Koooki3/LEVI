"""scripts/check_skills.py: frontmatter, ORIGIN.json hashes and Codex discovery links of the repo skills."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_skills", ROOT / "scripts" / "check_skills.py"
)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


def make_skill(
    root: Path, name: str, frontmatter: str, extra: dict[str, str] | None = None
) -> Path:
    d = root / ".claude" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\n{frontmatter}\n---\n\n# {name}\n", encoding="utf-8"
    )
    for rel, body in (extra or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(body, encoding="utf-8")
    return d


def origin(skill: Path, **overrides) -> None:
    files = {
        str(p.relative_to(skill)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(skill.rglob("*"))
        if p.is_file() and p.name != "ORIGIN.json"
    }
    data = {"name": skill.name, "files": files, **overrides}
    (skill / "ORIGIN.json").write_text(json.dumps(data), encoding="utf-8")


def test_the_repositorys_own_skills_pass():
    assert cs.check_all(ROOT) == []


def test_a_plain_description_with_a_colon_is_rejected_with_file_line_and_fix(tmp_path):
    make_skill(
        tmp_path, "bad", "name: bad\ndescription: Run a loop in LEVI work: tune a thing"
    )
    errors = cs.check_all(tmp_path)
    assert len(errors) == 1
    assert "SKILL.md:3" in errors[0] and "'>-'" in errors[0] and "quotes" in errors[0]


def test_a_plain_description_with_a_comment_marker_is_rejected(tmp_path):
    make_skill(
        tmp_path, "hashy", "name: hashy\ndescription: Tune a thing # not a comment"
    )
    assert any("' #'" in e for e in cs.check_all(tmp_path))


def test_block_scalar_and_quoted_descriptions_with_colons_pass(tmp_path):
    make_skill(
        tmp_path,
        "blocky",
        "name: blocky\ndescription: >-\n  Run a loop in LEVI work: tune a thing,\n  then confirm.",
    )
    make_skill(
        tmp_path, "quoted", 'name: quoted\ndescription: "Run a loop: tune a thing"'
    )
    assert cs.check_all(tmp_path) == []


def test_name_must_match_the_directory_and_the_pattern(tmp_path):
    make_skill(tmp_path, "one", "name: other\ndescription: ok")
    make_skill(tmp_path, "Two", "name: Two\ndescription: ok")
    errors = "\n".join(cs.check_all(tmp_path))
    assert "must equal the directory name" in errors and "lowercase letters" in errors


def test_missing_frontmatter_or_description_or_overlong_description(tmp_path):
    d = tmp_path / ".claude" / "skills" / "nofm"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
    make_skill(tmp_path, "nodesc", "name: nodesc")
    make_skill(tmp_path, "long", "name: long\ndescription: >-\n  " + "word " * 300)
    errors = "\n".join(cs.check_all(tmp_path))
    assert (
        "must start with a '---'" in errors
        and "no non-empty 'description'" in errors
        and "limit 1024" in errors
    )


def test_origin_hash_mismatch_missing_and_unlisted_files_are_reported(tmp_path):
    skill = make_skill(
        tmp_path, "prov", "name: prov\ndescription: ok", {"references/a.md": "a\n"}
    )
    origin(skill)
    assert cs.check_all(tmp_path) == []
    (skill / "references/a.md").write_text("changed\n", encoding="utf-8")
    assert any(
        "does not match its recorded sha256" in e for e in cs.check_all(tmp_path)
    )
    (skill / "references/a.md").unlink()
    assert any("does not exist" in e for e in cs.check_all(tmp_path))
    (skill / "references/b.md").write_text("b\n", encoding="utf-8")
    assert any("exists but is not listed" in e for e in cs.check_all(tmp_path))


def test_discovery_links_must_be_symlinks_to_the_skill_and_resolve(tmp_path):
    make_skill(tmp_path, "linked", "name: linked\ndescription: ok")
    agents = tmp_path / ".agents" / "skills"
    agents.mkdir(parents=True)
    os.symlink("../../.claude/skills/linked", agents / "linked")
    assert cs.check_all(tmp_path) == []
    os.symlink("../../.claude/skills/missing", agents / "missing")
    (agents / "copy").mkdir()
    os.symlink("../../.claude/skills/linked", agents / "wrongname")
    errors = "\n".join(cs.check_all(tmp_path))
    assert "without SKILL.md" in errors
    assert "must be a symlink" in errors and "expected" in errors


def test_main_exit_codes(tmp_path, capsys):
    make_skill(tmp_path, "good", "name: good\ndescription: ok")
    assert cs.main(["--root", str(tmp_path)]) == 0
    make_skill(tmp_path, "bad", "name: bad\ndescription: a: b")
    assert cs.main(["--root", str(tmp_path)]) == 1
    assert "ERROR" in capsys.readouterr().out
