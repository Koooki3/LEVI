"""The task journal's per-thread SQLite connections do not outlive their thread.

A live core runs many short runs, each on its own thread (plus an episode pool
and a heartbeat), and was seen holding ~30 ``workbench.sqlite3`` descriptors and
as many ``-wal`` ones for ~28 runs: a finished thread's connection was dropped
but never closed. The soft descriptor limit is 1024, so a long unattended
service would run out.
"""

import os
import threading
import time

from conftest import scaled
from test_agent_workbench import bench  # noqa: F401  (fixture)

from levi.agent.planning import approve
from levi.agent.store import Store, close_thread_connections


def journal_fds(path) -> int:
    """Open descriptors of this process on exactly ``path`` (not its -wal/-shm)."""
    target = os.path.realpath(path)
    count = 0
    for name in os.listdir("/proc/self/fd"):
        try:
            if os.path.realpath(f"/proc/self/fd/{name}") == target:
                count += 1
        except OSError:
            continue
    return count


def wait_for(condition, seconds=30):
    deadline = time.monotonic() + scaled(seconds)
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def test_threads_that_used_the_store_leave_no_descriptor(tmp_path):
    store = Store(tmp_path)
    database = store.root / "workbench.sqlite3"

    def use():
        store.put("note", "a", {"x": 1})
        assert store.get("note", "a") == {"x": 1}

    def churn(n):
        for _ in range(n):
            thread = threading.Thread(target=use)
            thread.start()
            thread.join()

    churn(3)
    baseline = journal_fds(database)
    churn(30)
    assert journal_fds(database) <= baseline
    # The main thread's own connection is unaffected and still works.
    assert store.get("note", "a") == {"x": 1}


def test_close_thread_connections_is_explicit_and_reopens(tmp_path):
    store = Store(tmp_path)
    database = store.root / "workbench.sqlite3"
    assert journal_fds(database) == 1
    close_thread_connections()
    assert journal_fds(database) == 0
    store.put("note", "b", {"y": 2})  # a fresh connection on demand
    assert store.get("note", "b") == {"y": 2}
    assert journal_fds(database) == 1


def test_finished_runs_do_not_accumulate_connections(bench):  # noqa: F811
    """Run after run on the real executor thread: the journal's descriptor
    count stays flat instead of growing by one per run."""
    wb, context = bench
    database = wb.store.root / "workbench.sqlite3"
    quiet = threading.active_count()

    def one_run():
        run = wb.plan(context)
        run = approve(wb, run["id"], 1, "fixture-human")
        wb.launch(run["id"], pilot=True)
        # the executor, its heartbeat and the episode pool have all ended
        assert wait_for(lambda: threading.active_count() <= quiet)
        assert wb.store.get("runs", run["id"])["status"] == "waiting_for_review"

    one_run()
    baseline = journal_fds(database)
    for _ in range(6):
        one_run()
    assert journal_fds(database) <= baseline
