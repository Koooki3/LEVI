"""scripts/select_tests.py: a suggestion that widens instead of guessing."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "select_tests.py"


def load(tmp_path):
    spec = importlib.util.spec_from_file_location("select_tests_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    return module


def write(root: Path, files: dict[str, str]) -> list[str]:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf8")
    return sorted(files)


def test_a_test_is_selected_through_a_chain_of_module_level_imports(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {
            "levi/a.py": "X = 1\n",
            "levi/b.py": "from . import a\n",
            "levi/c.py": "VALUE = 2\n",
            "tests/test_b.py": "from levi import b\n",
            "tests/test_c.py": "from levi import c\n",
        },
    )
    out = sel.select(["levi/a.py"], files)
    assert out["pytest"]["tests"] == ["tests/test_b.py"]
    assert not out["pytest"]["widen"]


def test_an_import_inside_a_function_does_not_pull_in_the_importers_importers(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {
            "levi/report.py": "X = 1\n",
            "levi/service.py": "def route():\n    from . import report\n    return report.X\n",
            "tests/test_report.py": "from levi import report\n",
            "tests/test_service.py": "from levi import service\n",
        },
    )
    out = sel.select(["levi/report.py"], files)
    assert out["pytest"]["tests"] == ["tests/test_report.py"]


def test_a_module_level_import_chain_through_the_hub_is_followed(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {
            "levi/core.py": "X = 1\n",
            "levi/service.py": "from . import core\n",
            "tests/test_service.py": "from levi import service\n",
        },
    )
    assert sel.select(["levi/core.py"], files)["pytest"]["tests"] == [
        "tests/test_service.py"
    ]


def test_shared_configuration_widens_instead_of_selecting(tmp_path):
    sel = load(tmp_path)
    files = write(tmp_path, {"pyproject.toml": "", "tests/test_a.py": ""})
    out = sel.select(["pyproject.toml"], files)
    assert out["pytest"]["widen"] and "run all" in sel.render(out)


def test_a_data_file_no_test_names_widens(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {"levi/specs/x.json": "{}", "levi/a.py": "", "tests/test_a.py": "import levi.a"},
    )
    assert sel.select(["levi/specs/x.json"], files)["pytest"]["widen"]


def test_a_data_file_a_test_names_selects_that_test(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {
            "levi/specs/x.json": "{}",
            "tests/test_a.py": "SPEC = 'levi/specs/x.json'\n",
            "tests/test_b.py": "",
        },
    )
    out = sel.select(["levi/specs/x.json"], files)
    assert out["pytest"]["tests"] == ["tests/test_a.py"] and not out["pytest"]["widen"]


def test_typescript_follows_the_alias_and_relative_imports(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {
            "src/utils/a.ts": "export const a = 1;\n",
            "src/components/b.tsx": 'import { a } from "@/utils/a";\n',
            "src/components/__tests__/b.test.tsx": 'import b from "../b";\n',
            "src/components/__tests__/other.test.tsx": "",
        },
    )
    out = sel.select(["src/utils/a.ts"], files)
    assert out["bun"]["tests"] == ["src/components/__tests__/b.test.tsx"]


def test_shared_frontend_files_widen(tmp_path):
    sel = load(tmp_path)
    files = write(tmp_path, {"src/styles/ds.css": "", "src/x/__tests__/a.test.ts": ""})
    assert sel.select(["src/styles/ds.css"], files)["bun"]["widen"]


def test_a_test_that_asks_for_a_fixture_depends_on_what_the_fixture_imports(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {
            "levi/service.py": "APP = 1\n",
            "tests/conftest.py": (
                "import pytest\n\n@pytest.fixture\ndef client():\n"
                "    from levi import service\n    return service.APP\n"
            ),
            "tests/test_uses_client.py": "def test_x(client):\n    assert client\n",
            "tests/test_plain.py": "def test_y():\n    pass\n",
        },
    )
    out = sel.select(["levi/service.py"], files)
    assert out["pytest"]["tests"] == ["tests/test_uses_client.py"]


def test_a_module_named_in_a_string_is_started_not_imported(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {
            "levi/service.py": "APP = 1\n",
            "levi/core.py": "def run():\n    return 'levi.service:app'\n",
            "tests/test_core.py": "from levi import core\n",
        },
    )
    # core only names the service in a string: a service change does not reach it
    # through core's importers.
    assert sel.select(["levi/service.py"], files)["pytest"]["tests"] == []


def test_an_autouse_fixture_import_reaches_every_test_below_its_conftest(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {
            "levi/droid.py": "X = 1\n",
            "tests/conftest.py": (
                "import pytest\n\n@pytest.fixture(autouse=True)\ndef _no_droid():\n"
                "    from levi import droid\n    return droid.X\n"
            ),
            "tests/test_a.py": "def test_a():\n    pass\n",
            "tests/test_b.py": "def test_b():\n    pass\n",
        },
    )
    out = sel.select(["levi/droid.py"], files)
    assert out["pytest"]["tests"] == ["tests/test_a.py", "tests/test_b.py"]


def test_importing_a_submodule_depends_on_its_parent_package_init(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {
            "levi/pkg/__init__.py": "READY = True\n",
            "levi/pkg/sub.py": "Y = 1\n",
            "tests/test_sub.py": "from levi.pkg.sub import Y\n",
        },
    )
    assert sel.select(["levi/pkg/__init__.py"], files)["pytest"]["tests"] == [
        "tests/test_sub.py"
    ]


def test_a_deleted_module_nobody_imports_widens_even_when_a_test_mentions_it(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {"levi/a.py": "", "tests/test_a.py": "# mentions levi.old_name in a message\n"},
    )
    assert sel.select(["levi/gone.py"], files)["pytest"]["widen"]
    named = sel.select(["levi/old_name.py"], files)["pytest"]
    # A mention in a comment is not an import: it selects that test but
    # does not stand in for finding the importers, so the suite still widens.
    assert named["widen"]
    assert named["tests"] == ["tests/test_a.py"]


def test_an_unmapped_file_widens_both_groups(tmp_path):
    sel = load(tmp_path)
    files = write(tmp_path, {".github/workflows/test.yml": "", "tests/test_a.py": ""})
    out = sel.select([".github/workflows/test.yml"], files)
    assert out["pytest"]["widen"] and out["bun"]["widen"]


def test_a_source_a_test_reads_by_path_pieces_selects_that_test(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {
            "src/app/globals.css": "",
            "src/app/__tests__/css.test.ts": 'readFileSync(join(dir, "../globals.css"))\n',
            "src/app/__tests__/other.test.ts": "",
        },
    )
    out = sel.select(["src/app/globals.css"], files)
    assert out["bun"]["tests"] == ["src/app/__tests__/css.test.ts"]


def test_a_bare_import_of_a_helper_in_another_tests_directory_is_followed(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {
            "tests/live_helpers.py": "H = 1\n",
            "tests/automatic/test_x.py": "from live_helpers import H\n",
            "tests/automatic/test_y.py": "",
        },
    )
    assert sel.select(["tests/live_helpers.py"], files)["pytest"]["tests"] == [
        "tests/automatic/test_x.py"
    ]


def test_a_git_failure_is_an_error_not_an_empty_answer(tmp_path):
    import pytest

    sel = load(tmp_path)
    with pytest.raises(SystemExit):
        sel.git("rev-parse", "--verify", "no-such-ref-anywhere")


def test_a_deleted_module_a_source_still_imports_selects_the_tests_of_that_source(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {
            "levi/b.py": "from levi.old import X\n",
            "tests/test_b.py": "from levi.b import f\n",
            "tests/test_z.py": "# mentions levi.old in a comment\n",
        },
    )
    out = sel.select(["levi/old.py"], files)["pytest"]
    # test_b breaks at import through levi/b.py; a stray mention must not hide it.
    assert out["tests"] == ["tests/test_b.py", "tests/test_z.py"]
    assert not out["widen"]


def test_a_deleted_module_imported_from_its_package_or_relatively_is_found(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {
            "levi/p/__init__.py": "",
            "levi/p/sib.py": "from . import old\n",
            "tests/test_e.py": "import levi.p.old\n",
            "tests/test_f.py": "from levi.p import (\n    other,\n    old,\n)\n",
            "tests/test_g.py": "from levi.p import sib\n",
            "tests/test_h.py": "from levi.p import older\n",
        },
    )
    out = sel.select(["levi/p/old.py"], files)["pytest"]
    assert out["tests"] == ["tests/test_e.py", "tests/test_f.py", "tests/test_g.py"]


def test_a_deleted_test_file_other_tests_import_by_bare_name_selects_them(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {
            "tests/test_uses.py": "from test_gone import NAME\n",
            "tests/test_other.py": "def test_x():\n    pass\n",
        },
    )
    out = sel.select(["tests/test_gone.py"], files)["pytest"]
    assert out["tests"] == ["tests/test_uses.py"]
    assert not out["widen"]


def test_a_deleted_test_file_nothing_imports_widens_and_is_not_listed(tmp_path):
    sel = load(tmp_path)
    files = write(tmp_path, {"tests/test_i.py": "# see test_gone.py\n"})
    out = sel.select(["tests/test_gone.py"], files)["pytest"]
    assert "tests/test_gone.py" not in out["tests"]
    assert out["widen"]


def test_a_deleted_module_imported_relatively_from_another_package_is_followed(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {
            "levi/__init__.py": "",
            "levi/events/__init__.py": "",
            "levi/agent/__init__.py": "",
            "levi/agent/signals.py": "from ..events import gripper\n",
            "levi/top.py": "from .events.gripper import X\n",
            "tests/test_signals.py": "from levi.agent import signals\n",
            "tests/test_top.py": "from levi import top\n",
            # A mention alone must not switch the widen off or stand in for these.
            "tests/test_note.py": "# levi.events.gripper was here\n",
        },
    )
    out = sel.select(["levi/events/gripper.py"], files)["pytest"]
    assert {"tests/test_signals.py", "tests/test_top.py"} <= set(out["tests"])
    assert not out["widen"]


def test_a_deleted_module_only_mentioned_in_text_still_widens(tmp_path):
    sel = load(tmp_path)
    files = write(
        tmp_path,
        {"levi/a.py": "", "tests/test_z.py": "# levi.old is gone\n"},
    )
    out = sel.select(["levi/old.py"], files)["pytest"]
    assert out["widen"]


def test_untracked_files_are_part_of_the_tree(tmp_path):
    import subprocess

    sel = load(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    write(tmp_path, {"levi/a.py": "", "tests/test_new.py": "from levi import a\n"})
    assert {"levi/a.py", "tests/test_new.py"} <= set(sel.tracked())
