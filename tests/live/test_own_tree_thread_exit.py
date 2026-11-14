"""A thread or child of our own vLLM that exits during the tree walk.

``OwnUnits.descendants`` lists ``<pid>/task`` and then reads each
``task/<tid>/children``. A thread that exits between the two is gone from
``/proc`` and the read raises ``FileNotFoundError``; the live controller turned
that into ``probe_error`` ("the model process tree cannot be read") and yielded
the GPU although nothing was unreadable (CI run 43605351723, with the threaded
fake vLLM; a real vLLM starts and ends threads all the time). The walk now
skips what vanished after it was listed and still raises for what is there but
unreadable (fail closed). These cases use a fake kernel tree only; no real
process is read.
"""

import shutil
from pathlib import Path

import pytest

from levi.own_units import OwnUnits

LEADER = 4_100_000
WORKER = LEADER + 7  # a thread of the leader
CHILD = LEADER + 1  # the engine process
CHILD2 = LEADER + 2  # a second child, still alive
GRAND = LEADER + 3  # a child of CHILD2


def task(root, pid, tid, kids=()):
    path = root / str(pid) / "task" / str(tid)
    path.mkdir(parents=True)
    (path / "children").write_text(" ".join(map(str, kids)))
    return path


def after_read(monkeypatch, watched, action):
    """Run ``action`` right after ``watched`` is read: the entry vanishes
    after the walk has seen it."""
    read = Path.read_text

    def wrapper(path, *args, **kwargs):
        text = read(path, *args, **kwargs)
        if path == watched:
            action()
        return text

    monkeypatch.setattr(Path, "read_text", wrapper)


def before_read(monkeypatch, watched, action):
    """Run ``action`` right before ``watched`` is read."""
    read = Path.read_text

    def wrapper(path, *args, **kwargs):
        if path == watched:
            action()
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", wrapper)


def test_a_thread_that_exits_mid_walk_is_not_an_unreadable_tree(tmp_path, monkeypatch):
    root = tmp_path / "kernel"
    task(root, LEADER, LEADER, [CHILD])
    worker = task(root, LEADER, WORKER)
    task(root, CHILD, CHILD)
    before_read(monkeypatch, worker / "children", lambda: shutil.rmtree(worker))
    assert OwnUnits(root).descendants(LEADER) == {LEADER, CHILD}


def test_a_child_that_exits_between_the_levels_is_skipped(tmp_path, monkeypatch):
    root = tmp_path / "kernel"
    leader = task(root, LEADER, LEADER, [CHILD, CHILD2])
    task(root, CHILD, CHILD)
    task(root, CHILD2, CHILD2, [GRAND])
    task(root, GRAND, GRAND)
    # CHILD was listed by its parent, then it ended before its own level.
    after_read(
        monkeypatch, leader / "children", lambda: shutil.rmtree(root / str(CHILD))
    )
    # The live relatives are still found beyond the vanished one.
    assert OwnUnits(root).descendants(LEADER) == {LEADER, CHILD, CHILD2, GRAND}


def test_a_thread_that_exits_in_a_child_is_skipped_too(tmp_path, monkeypatch):
    root = tmp_path / "kernel"
    task(root, LEADER, LEADER, [CHILD])
    task(root, CHILD, CHILD, [GRAND])
    worker = task(root, CHILD, WORKER)
    task(root, GRAND, GRAND)
    before_read(monkeypatch, worker / "children", lambda: shutil.rmtree(worker))
    assert OwnUnits(root).descendants(LEADER) == {LEADER, CHILD, GRAND}


def test_the_asked_pid_without_a_task_directory_still_raises(tmp_path):
    root = tmp_path / "kernel"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        OwnUnits(root).descendants(LEADER)


def test_the_asked_pid_that_vanishes_mid_walk_still_raises(tmp_path, monkeypatch):
    # The pid we were asked about going is not "no descendants": not even its
    # main thread directory vanishing after the listing is skipped.
    root = tmp_path / "kernel"
    main = task(root, LEADER, LEADER, [CHILD])
    before_read(
        monkeypatch, main / "children", lambda: shutil.rmtree(root / str(LEADER))
    )
    with pytest.raises(FileNotFoundError):
        OwnUnits(root).descendants(LEADER)


def test_a_missing_children_file_of_a_live_task_still_raises(tmp_path):
    # Fail closed stays: a task that is still listed but has no children
    # file (a kernel without CONFIG_PROC_CHILDREN) is not "no descendants".
    root = tmp_path / "kernel"
    (root / str(LEADER) / "task" / str(LEADER)).mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        OwnUnits(root).descendants(LEADER)


def test_a_missing_children_file_in_a_child_still_raises(tmp_path):
    root = tmp_path / "kernel"
    task(root, LEADER, LEADER, [CHILD])
    (root / str(CHILD) / "task" / str(CHILD)).mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        OwnUnits(root).descendants(LEADER)
