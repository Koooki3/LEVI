"""A child process for the header crash tests (run as a script).

``journal_header_child.py <dir> <point>``: start a run whose header names
both modes and SIGKILL itself at ``<point>``:

- ``after_lock``: the run folder exists and is locked, no journal file;
- ``empty_file``: the journal file exists, nothing written;
- ``torn_header``: half the header's bytes are on disk (synced);
- ``after_plan``: ``plan.json`` is on disk, no header yet;
- ``after_header``: the whole header is on disk.
"""

import os
import signal
import sys

from levi.automatic import journal as J

RUN = "r-header-crash"
MODES = {"reset_mode": "human_assisted", "scene_check": "provider"}
PLAN_BODY = {"reset_mode": "human_assisted", "scene_check": "provider", "n": 1}


def die():
    os.kill(os.getpid(), signal.SIGKILL)


def main(directory, point):
    if point == "after_lock":
        take = J.Journal._take_lock

        def lock_then_die(folder):
            take(folder)
            die()

        J.Journal._take_lock = staticmethod(lock_then_die)
    elif point == "empty_file":
        J.Journal.append = lambda self, *a, **k: die()
    elif point == "torn_header":

        def half(self, data):
            os.write(self._fd, data[: len(data) // 2])
            os.fsync(self._fd)
            die()

        J.Journal._write = half
    elif point == "after_plan":
        J.Journal.append = lambda self, *a, **k: die()
    who = {
        "principal_kind": "orchestrator",
        "principal_id": "orch-test",
        "session_id": "s-child",
        "process": J.process_identity(),
        "command_id": None,
    }
    J.Journal.create(
        directory,
        run_id=RUN,
        plan_sha256=J.plan_digest(PLAN_BODY),
        authority=who,
        plan=PLAN_BODY,
        **MODES,
    )
    if point == "after_header":
        die()
    return 1  # every point dies before this


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
