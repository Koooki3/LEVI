"""``ProcReader.children`` when a thread exits between the listing and the read.

``children`` lists ``<pid>/task`` and reads ``task/<tid>/children``. A thread
that exits in between is gone from the kernel tree; the read used to raise
``FileNotFoundError``, which ``SoleClientGuard.prove`` turns into
``InterventionUnproven``: fail-closed, but a needless refusal of the real-robot
intervention proof. The reader now skips a thread whose directory is gone and
still raises for everything it cannot read (robot-safety path: it may only get
more tolerant of a listed thread that exited, never of an unreadable tree).
Fake kernel trees under ``tmp_path`` only; no real process is read.
"""

import shutil
from pathlib import Path

import pytest

from levi.automatic.fr3_intervention import ProcReader

PID = 4_100_000
WORKER = PID + 7
OTHER = PID + 8
CHILD = PID + 1
CHILD2 = PID + 2


def thread(root, pid, tid, kids=None):
    """``<pid>/task/<tid>`` with a children file (none when ``kids`` is None)."""
    path = root / str(pid) / "task" / str(tid)
    path.mkdir(parents=True)
    if kids is not None:
        (path / "children").write_text(" ".join(map(str, kids)))
    return path


def hook(monkeypatch, watched, action, *, before):
    """Run ``action`` right before (or after) ``watched`` is read."""
    read = Path.read_text

    def wrapper(path, *args, **kwargs):
        if before and path == watched:
            action()
        text = read(path, *args, **kwargs)
        if not before and path == watched:
            action()
        return text

    monkeypatch.setattr(Path, "read_text", wrapper)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "proc"


def test_a_thread_listed_then_gone_is_skipped_the_others_are_read(root, monkeypatch):
    thread(root, PID, PID, [CHILD])
    worker = thread(root, PID, WORKER, [])
    thread(root, PID, OTHER, [CHILD2])
    hook(
        monkeypatch,
        worker / "children",
        lambda: shutil.rmtree(worker),
        before=True,
    )
    assert ProcReader(root).children(PID) == {CHILD, CHILD2}


def test_a_thread_that_vanishes_after_its_children_were_read_keeps_them(
    root, monkeypatch
):
    worker = thread(root, PID, WORKER, [CHILD])
    thread(root, PID, PID, [])
    hook(
        monkeypatch,
        worker / "children",
        lambda: shutil.rmtree(worker),
        before=False,
    )
    assert ProcReader(root).children(PID) == {CHILD}


def test_a_thread_still_there_without_a_children_file_still_raises(root):
    # No CONFIG_PROC_CHILDREN: "cannot read" must not become "no children".
    thread(root, PID, PID, [CHILD])
    thread(root, PID, WORKER, None)
    with pytest.raises(FileNotFoundError):
        ProcReader(root).children(PID)


def test_a_main_thread_without_a_children_file_still_raises(root):
    thread(root, PID, PID, None)
    with pytest.raises(FileNotFoundError):
        ProcReader(root).children(PID)


def test_the_main_thread_vanishing_mid_read_still_raises(root, monkeypatch):
    """The asked process going away is not "no children", even though the
    worker threads of that process are skipped when they are gone too."""
    main = thread(root, PID, PID, [CHILD])
    thread(root, PID, WORKER, [])
    hook(
        monkeypatch,
        main / "children",
        lambda: shutil.rmtree(root / str(PID)),
        before=True,
    )
    with pytest.raises(FileNotFoundError):
        ProcReader(root).children(PID)


def test_every_thread_gone_including_the_main_one_still_raises(root, monkeypatch):
    # The process exits right after the listing: every thread's read fails.
    # Workers are skipped, but the main thread (always listed) raises, in
    # whatever order the listing comes.
    thread(root, PID, WORKER, [])
    thread(root, PID, OTHER, [])
    thread(root, PID, PID, [])
    read = Path.read_text
    gone = []

    def wrapper(path, *args, **kwargs):
        if not gone:
            gone.append(True)
            shutil.rmtree(root / str(PID))
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", wrapper)
    with pytest.raises(FileNotFoundError):
        ProcReader(root).children(PID)


def test_a_pid_without_a_task_directory_has_no_children_as_before(root):
    root.mkdir()
    assert ProcReader(root).children(PID) == set()


def test_another_read_error_is_not_swallowed(root, monkeypatch):
    thread(root, PID, PID, [CHILD])
    worker = thread(root, PID, WORKER, [])
    read = Path.read_text

    def wrapper(path, *args, **kwargs):
        if path == worker / "children":
            raise PermissionError(13, "denied", str(path))
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", wrapper)
    with pytest.raises(PermissionError):
        ProcReader(root).children(PID)


def test_a_vanished_thread_does_not_turn_the_proof_walk_into_a_pass(root, monkeypatch):
    """Through ``SoleClientGuard._own_pids``: the walk still finds the live
    descendants, and a missing children file is still an error there."""
    from levi.automatic.fr3_intervention import SoleClientGuard

    thread(root, PID, PID, [CHILD])
    worker = thread(root, PID, WORKER, [])
    thread(root, CHILD, CHILD, [])
    hook(
        monkeypatch,
        worker / "children",
        lambda: shutil.rmtree(worker),
        before=True,
    )
    guard = SoleClientGuard.__new__(SoleClientGuard)
    guard.client_pids = (PID,)
    guard.reader = ProcReader(root)
    assert guard._own_pids() == {PID, CHILD}
    monkeypatch.undo()
    (root / str(CHILD) / "task" / str(CHILD) / "children").unlink()
    with pytest.raises(FileNotFoundError):
        guard._own_pids()
