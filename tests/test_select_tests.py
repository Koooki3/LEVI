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
