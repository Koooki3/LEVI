"""``trim_cache`` never deletes through a symlink.

It removes ``<state>/agent/datasets/<dataset>/runs/<run>/{input,evidence}``.
``shutil.rmtree`` refuses a symlink only as the last path component, so a
symlinked dataset, ``runs`` or run folder used to make it delete inside the
link's target, outside the workbench included. Every deletion here happens in
``tmp_path``: ``state`` is a fake workbench state, ``outside`` stands for any
folder that is not the cache, and each test asserts that nothing under
``outside`` (or the in-root run that a link points at) changed.
"""

import os
import shutil
import time
from pathlib import Path

import pytest

from levi.live import resources

OLD = time.time() - 10 * 3600  # older than the 6 h default minimum age


def age(path):
    os.utime(path, (OLD, OLD))


def run_folder(runs: Path, run: str, size=1000):
    """``runs/<run>/{input,evidence}`` with a file in each, all old."""
    for part in ("input", "evidence"):
        folder = runs / run / part
        folder.mkdir(parents=True)
        (folder / "f").write_bytes(b"0" * size)
        age(folder)


def snapshot(root: Path):
    """Every path under ``root`` (links not followed) with its size."""
    out = {}
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(dirpath, name)
            out[str(path.relative_to(root))] = (
                path.is_symlink(),
                0 if path.is_dir() and not path.is_symlink() else path.lstat().st_size,
            )
    return out


@pytest.fixture
def world(tmp_path):
    """A fake state with one normal run ``good``, and an ``outside`` tree."""
    state = tmp_path / "state"
    datasets = state / "agent" / "datasets"
    run_folder(datasets / "d" / "runs", "good")
    outside = tmp_path / "outside"
    # Shaped like a dataset folder so that a followed link would find victims.
    run_folder(outside / "runs", "victim")
    run_folder(outside / "runs" / "victim" / "nested", "deeper")
    assert state.resolve().is_relative_to(tmp_path.resolve())
    return state, datasets, outside


def trim(state, **kwargs):
    # A cap of 0 makes every old run folder a candidate.
    return resources.trim_cache(state, 0, **kwargs)


def test_a_normal_run_is_still_removed(world):
    state, datasets, outside = world
    before = snapshot(outside)
    result = trim(state)
    assert result["removed"] == 2 and result["skipped"] == 0
    assert result["before"] == 2000 and result["after"] == 0
    assert not (datasets / "d/runs/good/input").exists()
    assert not (datasets / "d/runs/good/evidence").exists()
    assert snapshot(outside) == before


def test_a_symlinked_run_folder_is_not_followed(world):
    state, datasets, outside = world
    runs = datasets / "d" / "runs"
    (runs / "linked").symlink_to(outside / "runs" / "victim")
    before = snapshot(outside)
    result = trim(state)
    assert snapshot(outside) == before, "something outside the cache was deleted"
    assert (outside / "runs/victim/input/f").exists()
    assert (runs / "linked").is_symlink()
    # The normal sibling went, the link's two targets were skipped, not counted.
    assert result["removed"] == 2 and result["skipped"] == 2
    assert result["before"] == 2000
    assert not (runs / "good/input").exists()


def test_a_symlinked_dataset_folder_is_not_followed(world):
    state, datasets, outside = world
    (datasets / "linked").symlink_to(outside)
    before = snapshot(outside)
    result = trim(state)
    assert snapshot(outside) == before, "something outside the cache was deleted"
    assert (datasets / "linked").is_symlink()
    assert result["removed"] == 2 and result["skipped"] >= 2
    assert not (datasets / "d/runs/good/input").exists()


def test_a_symlinked_runs_folder_is_not_followed(world):
    state, datasets, outside = world
    (datasets / "other").mkdir()
    (datasets / "other" / "runs").symlink_to(outside / "runs")
    before = snapshot(outside)
    result = trim(state)
    assert snapshot(outside) == before, "something outside the cache was deleted"
    assert result["removed"] == 2 and result["skipped"] >= 2


@pytest.mark.parametrize("link", ["run", "dataset", "runs"])
def test_the_resolved_path_is_a_second_guard_when_the_layer_check_is_blind(
    world, monkeypatch, link
):
    """Defence in depth: even if the per-folder symlink test said "not a link"
    (here forced), a path that resolves outside ``datasets`` is not deleted."""
    state, datasets, outside = world
    if link == "run":
        (datasets / "d" / "runs" / "linked").symlink_to(outside / "runs" / "victim")
    elif link == "dataset":
        (datasets / "linked").symlink_to(outside)
    else:
        (datasets / "other").mkdir()
        (datasets / "other" / "runs").symlink_to(outside / "runs")
    monkeypatch.setattr(resources.os.path, "islink", lambda path: False)
    before = snapshot(outside)
    result = trim(state)
    monkeypatch.undo()
    assert snapshot(outside) == before, "something outside the cache was deleted"
    assert result["removed"] == 2 and result["skipped"] >= 2


def test_a_link_to_another_run_inside_the_cache_is_not_followed(world):
    """The target resolves inside ``datasets`` but is another run's folder:
    deleting through the link would remove a run this entry does not name."""
    state, datasets, _ = world
    runs = datasets / "d" / "runs"
    run_folder(runs, "kept-by-nobody")
    (runs / "alias").symlink_to(runs / "kept-by-nobody")
    keep = ["kept-by-nobody"]
    result = trim(state, keep=keep)
    # ``keep`` protects the real folder; the alias must not bypass that.
    assert (runs / "kept-by-nobody/input/f").exists()
    assert (runs / "kept-by-nobody/evidence/f").exists()
    assert (runs / "alias").is_symlink()
    assert result["skipped"] == 2
    assert not (runs / "good/input").exists()


def test_a_symlinked_input_folder_is_skipped_not_counted_as_removed(world):
    state, datasets, outside = world
    runs = datasets / "d" / "runs"
    (runs / "half").mkdir()
    (runs / "half" / "input").symlink_to(outside / "runs" / "victim" / "input")
    age(runs / "half")
    before = snapshot(outside)
    result = trim(state)
    assert snapshot(outside) == before
    assert (runs / "half" / "input").is_symlink()
    assert result["removed"] == 2 and result["skipped"] == 1


def test_a_symlinked_datasets_root_is_the_configured_location(tmp_path):
    """``<state>/agent/datasets`` itself may be a link (state on another disk):
    it is resolved, not rejected, and the cache under it is trimmed as usual."""
    real = tmp_path / "disk2" / "datasets"
    run_folder(real / "d" / "runs", "good")
    state = tmp_path / "state"
    (state / "agent").mkdir(parents=True)
    (state / "agent" / "datasets").symlink_to(real)
    result = trim(state)
    assert result["removed"] == 2 and result["skipped"] == 0
    assert not (real / "d/runs/good/input").exists()


def test_a_run_swapped_for_a_link_after_the_scan_is_not_deleted(world, monkeypatch):
    """The scan can be old by the time a folder is deleted: it is checked again
    right before ``rmtree``."""
    state, datasets, outside = world
    runs = datasets / "d" / "runs"
    run_folder(runs, "swap")
    check = resources._inside_datasets
    seen = {}

    def swapping(base, target):
        ok = check(base, target)
        if target.parent.name == "swap" and target not in seen:
            seen[target] = True
            if len(seen) == 2:  # after both scan checks, before any delete
                shutil.rmtree(runs / "swap")
                (runs / "swap").symlink_to(outside / "runs" / "victim")
        return ok

    monkeypatch.setattr(resources, "_inside_datasets", swapping)
    before = snapshot(outside)
    result = trim(state)
    assert snapshot(outside) == before, "something outside the cache was deleted"
    assert (runs / "swap").is_symlink()
    assert result["skipped"] >= 1


def test_keep_still_matches_the_run_folder_name_exactly(world):
    state, datasets, _ = world
    runs = datasets / "d" / "runs"
    for name in ("r2", "r20"):
        run_folder(runs, name)
    result = trim(state, keep=["r2", ""])
    assert (runs / "r2/input").exists() and (runs / "r2/evidence").exists()
    assert not (runs / "r20/input").exists()
    assert not (runs / "good/input").exists()
    assert result["skipped"] == 0


def test_recent_and_capped_runs_behave_as_before(world):
    state, datasets, _ = world
    runs = datasets / "d" / "runs"
    run_folder(runs, "young")
    now = time.time()
    for part in ("input", "evidence"):
        os.utime(runs / "young" / part, (now, now))
    # Under the cap nothing goes; over it only old runs go, young ones stay.
    assert resources.trim_cache(state, 1)["removed"] == 0
    result = trim(state)
    assert result["removed"] == 2
    assert (runs / "young/input").exists()
