"""A thread of our own vLLM that exits during the tree walk.

``OwnUnits.descendants`` lists ``<pid>/task`` and then reads each
``task/<tid>/children``. A thread that exits between the two is gone from
``/proc`` and the read raises ``FileNotFoundError``; the live controller turns
that into ``probe_error`` ("the model process tree cannot be read") and yields
the GPU although nothing is unreadable (CI run 43605351723, with the threaded
fake vLLM). A real vLLM starts and ends threads all the time.

This is a known product defect (report: lab/levi-hub/reports/ci-live-proc-1114.md,
not versioned here): the test is ``xfail(strict=True)`` so a fix in
``levi/own_units.py`` turns it into a failure that asks for the marker to go.
It uses a fake kernel tree only; no real process is read.
"""

import shutil
from pathlib import Path

import pytest

from levi.own_units import OwnUnits

LEADER = 4_100_000
WORKER = LEADER + 7  # a thread of the leader
CHILD = LEADER + 1  # the engine process


def task(root, pid, tid, kids=()):
    path = root / str(pid) / "task" / str(tid)
    path.mkdir(parents=True)
    (path / "children").write_text(" ".join(map(str, kids)))
    return path


@pytest.mark.xfail(
    strict=True,
    raises=FileNotFoundError,
    reason="descendants() treats a thread that exited mid-walk as an unreadable tree",
)
def test_a_thread_that_exits_mid_walk_is_not_an_unreadable_tree(tmp_path, monkeypatch):
    root = tmp_path / "kernel"
    task(root, LEADER, LEADER, [CHILD])
    worker = task(root, LEADER, WORKER)
    task(root, CHILD, CHILD)
    read = Path.read_text

    def thread_exits_first(path, *args, **kwargs):
        if path == worker / "children":
            shutil.rmtree(worker)  # the thread ended after the listing
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", thread_exits_first)
    assert OwnUnits(root).descendants(LEADER) == {LEADER, CHILD}


def test_a_missing_children_file_of_a_live_task_still_raises(tmp_path):
    # Fail closed stays: a task that is still listed but has no children
    # file (a kernel without CONFIG_PROC_CHILDREN) is not "no descendants".
    root = tmp_path / "kernel"
    (root / str(LEADER) / "task" / str(LEADER)).mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        OwnUnits(root).descendants(LEADER)
