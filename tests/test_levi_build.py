"""``levi build`` installs the frontend dependencies first when
``node_modules`` does not match ``bun.lock``/``package.json``, and a failed
build points at them. Bun is never run: ``subprocess.call`` is replaced."""

import json
import os
import time

import pytest

from levi import bootstrap, doctor
from levi import cli as levi_cli


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    project = tmp_path / "checkout"
    project.mkdir()
    (project / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"next": "1", "lucide-react": "1"},
                "devDependencies": {"motion": "1"},
            }
        )
    )
    (project / "bun.lock").write_text("{}")
    (project / "src").mkdir()
    (project / "src/page.tsx").write_text("x")
    for module in (bootstrap, levi_cli, doctor):
        monkeypatch.setattr(module, "PROJECT", project)
    return project


def install_all(project, names=("next", "lucide-react", "motion")):
    for name in names:
        (project / "node_modules" / name).mkdir(parents=True, exist_ok=True)
        (project / "node_modules" / name / "package.json").write_text("{}")
    bootstrap.mark_frontend_deps(project)
    # The lockfile and package.json are older than the install.
    past = time.time() - 60
    for name in ("bun.lock", "package.json"):
        os.utime(project / name, (past, past))


@pytest.fixture
def calls(monkeypatch, checkout):
    """Every command ``levi build`` runs, answered by ``codes`` (default 0);
    the build makes a ``.next`` the stamp can be written into."""
    ran, codes = [], {}

    def call(cmd, cwd=None):
        ran.append(cmd[1:])
        if cmd[1:3] == ["run", "build"]:
            (checkout / ".next").mkdir(exist_ok=True)
        if cmd[1] == "install" and codes.get("install", 0) == 0:
            install_all(checkout)
        return codes.get(cmd[1] if cmd[1] == "install" else "build", 0)

    monkeypatch.setattr(levi_cli.subprocess, "call", call)
    return ran, codes


def test_the_dependency_check_names_what_is_off(checkout):
    assert bootstrap.frontend_deps_problem() == "node_modules is missing"
    install_all(checkout)
    assert bootstrap.frontend_deps_problem() == ""
    (checkout / "node_modules/motion/package.json").unlink()
    (checkout / "node_modules/lucide-react/package.json").unlink()
    assert (
        bootstrap.frontend_deps_problem() == "node_modules lacks lucide-react, motion"
    )
    install_all(checkout)
    (checkout / "bun.lock").write_text('{"changed": true}')
    future = time.time() + 5
    os.utime(checkout / "bun.lock", (future, future))
    assert bootstrap.frontend_deps_problem() == "bun.lock is newer than node_modules"


def test_build_installs_missing_dependencies_first(checkout, calls, capsys):
    ran, _codes = calls
    install_all(checkout, names=("next",))  # lucide-react and motion missing
    assert levi_cli.build_frontend("/x/bun") == 0
    assert ran == [["install", "--frozen-lockfile"], ["run", "build"]]
    assert "lacks lucide-react, motion" in capsys.readouterr().out
    assert bootstrap.frontend_deps_problem() == ""
    assert (checkout / doctor.BUILD_STAMP).is_file()


def test_build_with_current_dependencies_only_builds(checkout, calls):
    ran, _codes = calls
    install_all(checkout)
    assert levi_cli.build_frontend("/x/bun") == 0
    assert ran == [["run", "build"]]


def test_a_failed_install_stops_before_the_build(checkout, calls, capsys):
    ran, codes = calls
    codes["install"] = 1
    assert levi_cli.build_frontend("/x/bun") == 1
    assert ran == [["install", "--frozen-lockfile"]]
    err = capsys.readouterr().err
    assert "bun.lock" in err and "levi setup" in err
    assert not (checkout / doctor.BUILD_STAMP).exists()


def test_a_failed_build_points_at_the_dependencies(checkout, calls, capsys):
    ran, codes = calls
    install_all(checkout)
    codes["build"] = 1
    assert levi_cli.build_frontend("/x/bun") == 1
    assert ran == [["run", "build"]]
    err = capsys.readouterr().err
    assert "frontend dependencies" in err and "levi setup" in err
    assert not (checkout / doctor.BUILD_STAMP).exists()
