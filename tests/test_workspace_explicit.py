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


def test_clean_with_apply_and_an_explicit_workspace_never_runs_the_process_wide_pool_sweep(
    tmp_path, monkeypatch
):
    """The sweep deletes expired pool exports and job files of the process-wide
    workspace; asked to clean workspace A it must not touch workspace B's pool."""
    from levi.pool import cleanup as pool_cleanup

    calls = []
    monkeypatch.setattr(
        pool_cleanup, "sweep", lambda dry_run=True: calls.append(dry_run) or {"bytes": 1}
    )
    monkeypatch.setattr(maintenance, "PROJECT", tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    ws = make_workspace(tmp_path / "a", registered=[], leftovers=[])
    maintenance.clean(True, ws)
    assert calls == []
    # the legacy call still sweeps, with the apply flag it was given
    monkeypatch.setattr(maintenance, "STATE", ws.state)
    # The legacy call lists the checkout's real caches; none may be removed here.
    monkeypatch.setattr(maintenance, "candidates", lambda *args, **kwargs: [])
    maintenance.clean(True)
    assert calls == [False]


def test_an_explicit_workspace_never_lists_the_checkouts_own_caches(tmp_path):
    ws = make_workspace(tmp_path / "ws", registered=[], leftovers=[])
    (ws.root / ".cache/levi").mkdir(parents=True)
    report = maintenance.clean(False, ws)
    listed = {entry["path"] for entry in report["entries"]}
    assert listed == {str(ws.root / ".cache/levi")}
    assert report["pool"]["removed"] == [] and report["pool"]["dry_run"] is True


@pytest.mark.parametrize("root", [Path("/"), Path.home()])
def test_an_explicit_workspace_at_the_filesystem_root_or_home_is_refused(root):
    ws = Workspace(root, root / "outputs/LEVI/workbench", root / ".cache/levi")
    with pytest.raises(ValueError, match="not a LEVI workspace root"):
        maintenance.clean(False, ws)


def test_apply_on_an_explicit_folder_that_is_not_a_workspace_is_refused(tmp_path):
    ws = Workspace.at(tmp_path / "not-a-workspace")
    (ws.root / ".cache/levi").mkdir(parents=True)
    # A dry run may look; removing needs the workbench folder to exist.
    assert maintenance.clean(False, ws)["entries"]
    with pytest.raises(ValueError, match="not a LEVI workspace"):
        maintenance.clean(True, ws)
    assert (ws.root / ".cache/levi").is_dir()


def test_a_root_above_a_home_folder_or_a_moved_home_is_refused(tmp_path, monkeypatch):
    import pwd

    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    above = real_home.parent
    with pytest.raises(ValueError, match="not a LEVI workspace root"):
        maintenance.clean(False, Workspace(above, above / "outputs/LEVI/workbench", above / ".cache/levi"))
    # HOME pointing elsewhere does not unprotect the account's real home.
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(ValueError, match="not a LEVI workspace root"):
        maintenance.clean(False, Workspace(real_home, real_home / "outputs/LEVI/workbench", real_home / ".cache/levi"))


def test_a_state_outside_the_root_is_refused_even_when_it_exists(tmp_path):
    other = make_workspace(tmp_path / "real", registered=[], leftovers=[])
    root = tmp_path / "victim"
    (root / ".cache/levi").mkdir(parents=True)
    ws = Workspace(root, other.state, root / ".cache/levi")
    with pytest.raises(ValueError, match="not the standard layout"):
        maintenance.clean(True, ws)
    assert (root / ".cache/levi").is_dir()


def test_a_sibling_folder_is_refused_unless_it_is_a_levi_workspace(tmp_path):
    # Somebody else's folder on the shared account: state == root, no catalog.
    other = tmp_path / "someone"
    (other / ".cache/levi").mkdir(parents=True)
    with pytest.raises(ValueError, match="not the standard layout"):
        maintenance.clean(True, Workspace(other, other, other / ".cache/levi"))
    # The standard layout without LEVI's catalog is not enough to remove anything.
    ws = Workspace.at(other)
    ws.state.mkdir(parents=True)
    with pytest.raises(ValueError, match="not a LEVI workspace"):
        maintenance.clean(True, ws)
    assert (other / ".cache/levi").is_dir()


def test_orphans_called_directly_checks_the_workspace_too(tmp_path):
    p = tmp_path / "p"
    p.mkdir()
    with pytest.raises(ValueError):
        maintenance.orphans(Workspace(p, p, p / "c"))


def test_a_symlinked_folder_under_the_state_is_not_followed(tmp_path):
    ws = make_workspace(tmp_path / "ws", registered=[], leftovers=[])
    outside = tmp_path / "outside" / "empty_shell"
    outside.mkdir(parents=True)
    (ws.state / "views").symlink_to(tmp_path / "outside")
    maintenance.clean(True, ws)
    assert outside.is_dir()


def test_the_cli_offers_no_way_to_pass_a_workspace(monkeypatch):
    calls = []
    monkeypatch.setattr(maintenance, "clean", lambda *a, **k: calls.append((a, k)) or {})
    monkeypatch.setattr(sys, "argv", ["levi-clean"])
    assert maintenance.main() == 0
    assert calls == [((False,), {})]
    for extra in (["--workspace", "/x"], ["--ws", "/x"], ["--root", "/x"], ["/x"]):
        monkeypatch.setattr(sys, "argv", ["levi-clean", *extra])
        with pytest.raises(SystemExit):
            maintenance.main()


def test_no_product_code_passes_an_explicit_workspace():
    import re

    root = Path(__file__).resolve().parents[1]
    offenders = []
    for folder in ("levi", "backend"):
        for f in (root / folder).rglob("*.py"):
            if f.name == "maintenance.py" and f.parent.name == "levi":
                continue
            text = f.read_text(encoding="utf8", errors="replace")
            if "maintenance" not in text:
                continue
            for call in re.finditer(r"\b(?:clean|orphans)\(([^)]*)\)", text):
                if re.search(r"\bws\b|Workspace", call.group(1)):
                    offenders.append(f"{f.relative_to(root)}: {call.group(0)}")
    assert offenders == []


def test_the_catalog_reads_an_explicit_workspace_and_defaults_to_its_own(tmp_path, monkeypatch):
    from levi import catalog

    own = make_workspace(tmp_path / "own", registered=["mine"], leftovers=[])
    other = make_workspace(tmp_path / "other", registered=["theirs"], leftovers=[])
    (other.state / "dataset_aliases.json").write_text(json.dumps({"old": "theirs"}))
    monkeypatch.setattr(catalog, "STATE", own.state)
    assert set(catalog.datasets()) == {"mine"}
    assert set(catalog.datasets(other)) == {"theirs"}
    assert catalog.aliases(other) == {"old": "theirs"}
    assert catalog.aliases() == {}
