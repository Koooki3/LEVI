"""An explicit Workspace: no import side effects, two workspaces in one process,
and the same answers as the module-global path older callers use."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from levi import maintenance
from levi.workspace import Workspace


def make_workspace(root: Path, registered: list[str], leftovers: list[str]) -> Workspace:
    ws = Workspace.at(root)
    ws.state.mkdir(parents=True)
    (ws.state / "datasets.json").write_text(
        json.dumps({name: {"path": str(root / name)} for name in registered})
    )
    for name in registered + leftovers:
        (ws.state / "annotations" / name).mkdir(parents=True)
    return ws


def test_importing_the_workspace_module_reads_no_dotenv_and_creates_nothing(tmp_path):
    (tmp_path / ".env").write_text("LEVI_WORKSPACE=should-not-be-read\n")
    code = (
        "import sys, levi.workspace;"
        "print('levi.paths' in sys.modules, 'dotenv' in sys.modules)"
    )
    env = {**os.environ, "LEVI_WORKSPACE": str(tmp_path / "ws"), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=env, text=True, capture_output=True, check=True
    ).stdout.split()
    assert out == ["False", "False"]
    assert not (tmp_path / "ws").exists()


def test_the_layout_matches_the_process_default(tmp_path):
    from levi import paths

    assert Workspace.at(paths.ROOT) == Workspace.default()


def test_two_workspaces_in_one_process_do_not_interfere(tmp_path):
    a = make_workspace(tmp_path / "a", registered=["kept_a"], leftovers=["gone_a"])
    b = make_workspace(tmp_path / "b", registered=["kept_b"], leftovers=["gone_b1", "gone_b2"])
    results = {}

    def run(key, ws):
        results[key] = [row["dataset"] for row in maintenance.orphans(ws)["keeps_content"] + maintenance.orphans(ws)["removable_empty"]]

    threads = [threading.Thread(target=run, args=(k, w)) for k, w in (("a", a), ("b", b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results["a"] == ["gone_a"]
    assert results["b"] == ["gone_b1", "gone_b2"]


def test_the_explicit_and_the_module_global_paths_agree(tmp_path, monkeypatch):
    from levi import catalog

    ws = make_workspace(tmp_path / "w", registered=["kept"], leftovers=["gone", "other"])
    (ws.state / "dataset_aliases.json").write_text(json.dumps({"old": "kept", "dead": "gone"}))
    explicit = maintenance.orphans(ws)
    monkeypatch.setattr(maintenance, "STATE", ws.state)
    monkeypatch.setattr(catalog, "STATE", ws.state)
    assert maintenance.orphans() == explicit


def test_clean_checks_the_given_workspace_for_a_running_service(tmp_path):
    ws = make_workspace(tmp_path / "w", registered=[], leftovers=[])
    (ws.state / "server.pid").write_text(str(os.getpid()))
    with pytest.raises(ValueError, match="Stop the LEVI service"):
        maintenance.clean(False, ws)
    quiet = make_workspace(tmp_path / "q", registered=[], leftovers=[])
    report = maintenance.clean(False, quiet)
    assert report["applied"] is False and report["orphans"]["removable_empty"] == []


def test_the_layout_is_the_documented_one_not_just_a_shared_constant():
    from levi import workspace

    assert workspace.STATE_REL == "outputs/LEVI/workbench"
    assert workspace.CACHE_REL == ".cache/levi"


def test_clean_with_apply_touches_only_the_given_workspace_and_skips_the_pool_sweep(
    tmp_path, monkeypatch
):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(maintenance, "PROJECT", project)
    a = make_workspace(tmp_path / "a", registered=[], leftovers=["empty_a"])
    b = make_workspace(tmp_path / "b", registered=[], leftovers=["empty_b"])
    for ws, name in ((a, "mine"), (b, "theirs")):
        (ws.root / ".cache/levi").mkdir(parents=True)
        (ws.root / ".cache/levi" / name).write_text("x")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")

    report = maintenance.clean(True, a)

    assert not (a.root / ".cache/levi").exists()
    assert (b.root / ".cache/levi/theirs").exists() and outside.exists()
    assert (b.state / "annotations/empty_b").exists()
    assert report["pool"]["bytes"] == 0 and "skipped" in report["pool"]
