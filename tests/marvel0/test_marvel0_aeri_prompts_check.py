"""``scripts/check_aeri_prompts.py`` and ``levi/marvel0/aeri_prompts.json``: the
words and key tables of terminal D per AERI code version, and the read-only check
of a worktree against them. The real AERI worktrees of this machine are read when
they are there (the real function on the real files); the failures are shown on
copies of their sources in a temporary folder."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import check_aeri_prompts as check
import regen_aeri_prompts as regen

from levi.marvel0 import aeri_state as st

SCRIPT = ROOT / "scripts" / "check_aeri_prompts.py"
DATA = json.loads(check.PROMPTS.read_text())
FILES = tuple(DATA["profiles"]["harvest-5bc750ca"]["sources_sha256"])
SCREEN = ROOT / "tests" / "marvel0" / "fixtures" / "aeri_r08" / "screen.txt"
REAL = {
    "harvest-5bc750ca": "harvest-validation-fix",
    "bfdee2cf": "aeri-subtask-loop",
}


def worktree(name):
    for parent in Path(__file__).resolve().parents:
        folder = parent / "lab" / "worktrees" / name
        if folder.is_dir():
            return folder
    return None


def run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def tree(tmp_path):
    """A copy of the four source files of the harvest worktree (or, where it is
    not, of this repository's own: the same words)."""
    source = worktree("harvest-validation-fix")
    if source is None:
        pytest.skip("no harvest-validation-fix worktree on this machine")
    for rel in FILES:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source / rel, target)
    return tmp_path


@pytest.mark.parametrize("profile", sorted(REAL))
def test_the_real_worktrees_are_what_their_profiles_were_written_from(profile):
    folder = worktree(REAL[profile])
    if folder is None:
        pytest.skip(f"no {REAL[profile]} worktree on this machine")
    found = check.check(folder, DATA["profiles"][profile])
    assert {k: v for k, v in found.items() if v} == {}
    done = run("--worktree", folder, "--profile", profile)
    assert done.returncode == 0 and "is what it was written from" in done.stdout


def commit_tree(tmp_path, profile_id):
    """The pinned files of a profile as **its commit** has them (``git show``),
    or a skip where the commit is not in the clone (a shallow one)."""
    profile = DATA["profiles"][profile_id]
    commit = profile["commit"]
    have = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if have.returncode != 0:
        pytest.skip(f"commit {commit} is not in this clone")
    for rel in profile["sources_sha256"]:
        shown = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{commit}:{rel}"],
            capture_output=True,
            timeout=30,
            check=True,
        )
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(shown.stdout)
    return tmp_path


@pytest.mark.parametrize("profile_id", ["main-297a74c3", "main-ab38c7b1"])
def test_a_main_profile_is_what_its_pinned_commit_has_in_it(tmp_path, profile_id):
    """A ``main-*`` profile against the files **of its commit** (``git show``), not
    against this repository's own ``levi/automatic``: the page hosts a pinned
    worktree and never the code it runs in, so a change to this repository's AERI
    code must not break this test (it would only push people to regenerate the
    profile without reading what changed). The gate against the pinned worktrees
    is ``check_aeri_prompts.py``. Skipped where that commit is not in the clone (a
    shallow one)."""
    tree = commit_tree(tmp_path, profile_id)
    found = check.check(tree, DATA["profiles"][profile_id])
    assert {k: v for k, v in found.items() if v} == {}, (
        "the main profile no longer matches its own commit: regenerate it"
    )
    done = run("--worktree", tree, "--profile", profile_id)
    assert done.returncode == 0, done.stdout


def test_a_change_to_this_repositorys_own_aeri_code_is_not_this_files_business():
    """The relaxation, pinned: nothing in this file reads ``levi/automatic`` of the
    checkout it runs in."""
    source = Path(__file__).read_text()
    assert "check.check(" + "ROOT," not in source
    assert 'ROOT / "levi" ' + '/ "automatic"' not in source


def test_the_profile_the_page_pins_cannot_be_hosted_with_other_words(tree):
    (tree / "levi/automatic/console_text.py").write_text(
        (tree / "levi/automatic/console_text.py")
        .read_text()
        .replace("确认场景就绪后按 Enter 放行", "确认就绪后按回车放行", 1)
    )
    ok, detail = st.profile_matches(tree, "harvest-5bc750ca")
    assert (ok, detail) == (False, "differs:levi/automatic/console_text.py")
    found = check.check(tree, DATA["profiles"]["harvest-5bc750ca"])
    assert found["digest"] == ["levi/automatic/console_text.py"]
    assert any(item.startswith("zh.sub_release: now") for item in found["changed"])
    done = run("--worktree", tree)
    assert (
        done.returncode == 1
        and "CHANGED" in done.stdout
        and "zh.sub_release" in done.stdout
    )


def test_a_sentence_that_is_gone_or_new_is_named(tree):
    path = tree / "levi/automatic/console_text.py"
    text = path.read_text()
    text = text.replace('"sub_released":', '"sub_released_x":', 2)
    path.write_text(text)
    found = check.check(tree, DATA["profiles"]["harvest-5bc750ca"])
    assert (
        "zh.sub_released" in found["missing"] and "zh.sub_released_x" in found["added"]
    )
    assert "en.sub_released" in found["missing"]


def test_a_changed_key_table_is_named(tree):
    path = tree / "levi/automatic/console.py"
    path.write_text(
        path.read_text().replace('QUIT = ("q", "Q", "\\x04")', 'QUIT = ("q", "Q")')
    )
    found = check.check(tree, DATA["profiles"]["harvest-5bc750ca"])
    assert any(item.startswith("QUIT: now") for item in found["tables"])
    assert any("QUIT" in item for item in found["code"])


def test_a_confirmation_set_that_grew_is_named(tree):
    path = tree / "levi/automatic/console.py"
    text = path.read_text()
    path.write_text(
        text.replace(
            '        "vlm_unavailable",\n        "subtask_retry_limit",',
            '        "vlm_unavailable",\n        "subtask_retry_limit",\n        "camera_stale",',
            1,
        )
    )
    found = check.check(tree, DATA["profiles"]["harvest-5bc750ca"])
    assert "CONFIRM_REASONS: differs" in found["tables"]


def test_the_order_of_the_phrase_code_is_pinned(tree):
    path = tree / "levi/automatic/fr3_run.py"
    path.write_text(
        path.read_text().replace(
            'phrase += " after policy switch check"', 'phrase += " after policy check"'
        )
    )
    found = check.check(tree, DATA["profiles"]["harvest-5bc750ca"])
    assert any("after policy switch check" in item for item in found["code"])


def test_a_file_that_cannot_be_read_fails_with_two(tree):
    (tree / "levi/automatic/subtask_units.py").unlink()
    done = run("--worktree", tree)
    assert done.returncode == 2 and "UNREADABLE" in done.stdout


def test_an_unknown_profile_or_folder_is_two(tmp_path):
    assert run("--worktree", tmp_path, "--profile", "nope").returncode == 2
    assert run("--worktree", tmp_path / "none").returncode == 2


def test_the_sample_screen_of_the_real_run_has_no_unknown_line_for_its_own_profile():
    folder = worktree("harvest-validation-fix")
    if folder is None:
        pytest.skip("no harvest-validation-fix worktree on this machine")
    done = run("--worktree", folder, "--sample", SCREEN)
    assert done.returncode == 0, done.stdout
    assert check.unknown_lines(SCREEN, "harvest-5bc750ca", "zh") == []


def test_the_same_screen_is_not_the_pins_screen(tmp_path):
    """The pin has no startup home and no progress line: the page must not
    read r08's screen with the pin's words."""
    lines = check.unknown_lines(SCREEN, "bfdee2cf", "zh")
    assert any("首轮回位" in line for line in lines) and any(
        "当前正向子任务" in line for line in lines
    )


def test_the_script_writes_nothing(tree, tmp_path):
    before = sorted(p.name for p in tree.rglob("*"))
    run("--worktree", tree)
    assert sorted(p.name for p in tree.rglob("*")) == before


def test_the_regeneration_is_the_committed_file():
    """The words are copied from the sources, not typed: the committed file is
    what the regeneration writes (it reads the three code versions with git)."""
    for name in REAL.values():
        if worktree(name) is None:
            pytest.skip("the AERI worktrees are not all on this machine")
    done = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests" / "marvel0" / "regen_aeri_prompts.py"),
            "--check",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert regen.OUT == check.PROMPTS


def test_the_regeneration_module_imports_where_there_is_no_lab_folder():
    """CI has no lab/worktrees/: importing the module (as this file does) must not
    fail there, or every test run stops at collection (CI run 39721227486)."""
    source = (ROOT / "tests" / "marvel0" / "regen_aeri_prompts.py").read_text()
    fake = Path("/nonexistent-levi-checkout/tests/marvel0/regen_aeri_prompts.py")
    namespace = {"__file__": str(fake), "__name__": "regen_without_lab"}
    before = list(sys.path)
    try:
        exec(compile(source, str(fake), "exec"), namespace)  # noqa: S102 -- this repo's own module, under a path with no lab/
    finally:
        sys.path[:] = before
    assert namespace["LAB"] == fake.parents[2]
    assert namespace["PROFILES"]["main-297a74c3"]["repo"] == fake.parents[2]


# --- the takeover (``grasp_assist``) profile --------------------------------------------------


def test_only_the_main_ab38c7b1_profile_has_the_takeover_and_the_default_is_unchanged():
    caps = {
        pid: p["capabilities"]["grasp_assist"] for pid, p in DATA["profiles"].items()
    }
    assert caps == {
        "harvest-5bc750ca": False,
        "bfdee2cf": False,
        "main-297a74c3": False,
        "main-ab38c7b1": True,
    }
    # the takeover implies the reverse reset role it hands over to
    assert DATA["profiles"]["main-ab38c7b1"]["capabilities"]["reset_policy"] is True
    # the default profile (the site's [aeri] profile when it names none) is the
    # code of the real run, untouched by this one (decision 2026-10-21)
    assert DATA["default"] == "harvest-5bc750ca" == regen.build()["default"]
    assert st.DEFAULT_PROFILE == "harvest-5bc750ca"
    for pid in ("harvest-5bc750ca", "bfdee2cf", "main-297a74c3"):
        assert "assist_keys" not in DATA["profiles"][pid]
        assert not any(
            f.endswith("grasp_assist.py")
            for f in DATA["profiles"][pid]["sources_sha256"]
        )


def test_the_takeover_profile_pins_the_key_table_and_the_aborted_wait_cannot_resume():
    profile = DATA["profiles"]["main-ab38c7b1"]
    assert profile["assist_keys"] == {
        "x": "x+",
        "X": "x-",
        "y": "y+",
        "Y": "y-",
        "z": "z+",
        "Z": "z-",
        "c": "close",
        "u": "lift",
        "o": "open",
        "g": "handback",
        "a": "abort",
    }
    assert "levi/automatic/grasp_assist.py" in profile["sources_sha256"]
    assert profile["no_resume_reasons"] == [
        "startup_home_not_executed",
        "grasp_assist_aborted",
    ]


def test_a_changed_takeover_key_letter_is_named(tmp_path):
    tree = commit_tree(tmp_path, "main-ab38c7b1")
    path = tree / "levi/automatic/grasp_assist.py"
    path.write_text(path.read_text().replace('"g": "handback"', '"h": "handback"', 1))
    found = check.check(tree, DATA["profiles"]["main-ab38c7b1"])
    assert any(item.startswith("KEY_CHARS: now") for item in found["tables"])
    assert found["digest"] == ["levi/automatic/grasp_assist.py"]
    ok, detail = st.profile_matches(tree, "main-ab38c7b1")
    assert (ok, detail) == (False, "differs:levi/automatic/grasp_assist.py")


def test_a_takeover_profile_whose_key_table_is_gone_fails(tmp_path):
    tree = commit_tree(tmp_path, "main-ab38c7b1")
    path = tree / "levi/automatic/grasp_assist.py"
    path.write_text(path.read_text().replace("KEY_CHARS = {", "KEY_LETTERS = {", 1))
    found = check.check(tree, DATA["profiles"]["main-ab38c7b1"])
    assert "KEY_CHARS: gone" in found["tables"]
    assert any("KEY_CHARS = {" in item for item in found["code"])
