"""``levi build`` installs the frontend dependencies first when
``node_modules`` does not match ``bun.lock``/``package.json``, and a failed
build points at them. Bun is never run: ``subprocess.call`` is replaced."""

import json
import os
import time

import pytest

from levi import bootstrap, doctor, install
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
    # Nothing of this checkout serves, unless a test says so.
    monkeypatch.setattr(install, "serving", lambda ui_port=7860: "")
    return project


def install_all(project, names=("next", "lucide-react", "motion"), mark=True):
    """Fill node_modules; ``mark`` writes the stamp as ``levi setup`` does
    (a bare ``bun install`` writes none: ``levi build`` marks after it)."""
    for name in names:
        (project / "node_modules" / name).mkdir(parents=True, exist_ok=True)
        (project / "node_modules" / name / "package.json").write_text("{}")
    if mark:
        bootstrap.mark_frontend_deps(project)


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
            install_all(checkout, mark=False)  # bun writes no stamp
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


def test_the_stamp_compares_contents_not_times(checkout):
    install_all(checkout)
    # Same content, newer time (a checkout or pull rewrote it): nothing to do.
    future = time.time() + 60
    for name in ("bun.lock", "package.json"):
        data = (checkout / name).read_bytes()
        (checkout / name).write_bytes(data)
        os.utime(checkout / name, (future, future))
    assert bootstrap.frontend_deps_problem() == ""
    # Changed content, older time: install.
    (checkout / "bun.lock").write_text('{"changed": true}')
    past = time.time() - 3600
    os.utime(checkout / "bun.lock", (past, past))
    assert (
        bootstrap.frontend_deps_problem() == "bun.lock changed since the last install"
    )


def test_no_stamp_means_install_once_then_the_stamp_says(checkout, calls):
    ran, _codes = calls
    install_all(checkout)
    (checkout / bootstrap.DEPS_STAMP).unlink()
    # The folder is newer than the lockfile: still unknown, so install.
    assert "no record" in bootstrap.frontend_deps_problem()
    (checkout / bootstrap.DEPS_STAMP).write_text("not json")
    assert "no record" in bootstrap.frontend_deps_problem()
    (checkout / bootstrap.DEPS_STAMP).unlink()
    assert levi_cli.build_frontend("/x/bun") == 0
    assert ran == [["install", "--frozen-lockfile"], ["run", "build"]]
    stamp = json.loads((checkout / bootstrap.DEPS_STAMP).read_text())
    assert set(stamp) == {"bun.lock", "package.json"}
    ran.clear()
    assert levi_cli.build_frontend("/x/bun") == 0
    assert ran == [["run", "build"]]


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


def test_build_is_refused_while_this_checkout_serves(
    checkout, calls, capsys, monkeypatch
):
    """The product's and the live service's ``next start`` read this
    checkout's node_modules and .next: neither install nor build runs."""
    ran, _codes = calls
    monkeypatch.setattr(
        install, "serving", lambda ui_port=7860: "port 7860 is listening"
    )
    assert levi_cli.build_frontend("/x/bun") == levi_cli.EXIT_SERVING
    assert ran == []
    err = capsys.readouterr().err
    assert "levi stop" in err and "levi live stop" in err
    assert "--while-serving" in err
    assert not (checkout / doctor.BUILD_STAMP).exists()


def test_while_serving_builds_but_never_installs(checkout, calls, capsys, monkeypatch):
    ran, _codes = calls
    install_all(checkout, names=("next",))  # dependencies missing
    monkeypatch.setattr(
        install, "serving", lambda ui_port=7860: "port 7860 is listening"
    )
    assert levi_cli.build_frontend("/x/bun", while_serving=True) == 0
    assert ran == [["run", "build"]]
    err = capsys.readouterr().err
    assert "warning" in err and "lacks lucide-react, motion" in err
