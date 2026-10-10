"""A child process for the journal crash tests (run as a script).

``journal_child.py <dir> <mode> [<counter file>]``

- mode ``crash:<point>``: start a run, commit PREFLIGHT -> VERIFY_INITIAL,
  then run one non-idempotent motion transaction and SIGKILL itself at
  ``<point>`` (before_prepared, after_prepared, after_execute,
  after_acknowledged, after_committed). "Executing" appends one byte to
  the counter file (synced), standing in for a robot command;
- mode ``notes``: start a run and append notes until killed;
- mode ``hold``: start a run, print ``ready`` and sleep (holds the lock).
"""

import os
import signal
import sys
import time

from levi.automatic.journal import Journal, process_identity

RUN = "r-crash"
PLAN = "cd" * 32
EPISODE = f"{RUN}.forward.0001"


def authority():
    return {
        "principal_kind": "orchestrator",
        "principal_id": "orch-test",
        "session_id": "s-child",
        "process": process_identity(),
        "command_id": None,
    }


def crash_at(point, wanted):
    if point == wanted:
        os.kill(os.getpid(), signal.SIGKILL)


def main(directory, mode, counter=None):
    who = authority()
    journal = Journal.create(directory, run_id=RUN, plan_sha256=PLAN, authority=who)
    if mode == "hold":
        print("ready", flush=True)
        time.sleep(60)
        return 0
    if mode == "notes":
        n = 0
        while True:
            journal.note("heartbeat", str(n), authority=who)
            n += 1
    point = mode.split(":", 1)[1]
    journal.prepare(
        "VERIFY_INITIAL",
        "preflight_passed",
        kind="none",
        authority=who,
        control_epoch=1,
    )
    journal.commit(authority=who)
    crash_at(point, "before_prepared")
    journal.prepare(
        "FORWARD_ACTIVE",
        "scene_ready",
        kind="policy_steps",
        non_idempotent=True,
        step=0,
        authority=who,
        control_epoch=2,
        episode_id=EPISODE,
        episode_role="forward",
    )
    crash_at(point, "after_prepared")
    with open(counter, "ab") as handle:
        handle.write(b"x")
        handle.flush()
        os.fsync(handle.fileno())
    crash_at(point, "after_execute")
    journal.acknowledge("yes", "robot_server", "ok", authority=who)
    crash_at(point, "after_acknowledged")
    journal.commit(authority=who)
    crash_at(point, "after_committed")
    journal.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
