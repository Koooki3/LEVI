"""A child process for the end-to-end SIGKILL tests (run as a script).

``e2e_child.py <root> <point>``: one forward episode in the whole fake world
(real recorder layer), SIGKILLed while the forward rollout is sealed:

- ``before_prepared``, ``after_prepared``, ``after_execute``,
  ``after_acknowledged``, ``after_committed``: at that point of the
  ``recorder_seal`` transaction (FORWARD_FINALIZE -> ROBOT_HOME);
- ``before_marker``: inside the recorder, just before ``.complete``;
- ``after_marker``: inside the recorder, after ``.complete`` and before the
  manifest says complete.

No network, no robot, no GPU."""

import os
import signal
import sys
from pathlib import Path

from aeri_harness import RUN
from aeri_world import READY, make_config, world

from levi.automatic.adapters import events as ev


def main(root, point):
    box = {"marker": False}

    def kill():
        os.kill(os.getpid(), signal.SIGKILL)

    def io_hook(op, path):
        if Path(path).name == ".complete" and op == "marker":
            box["marker"] = True
            if point == "before_marker":
                kill()
        elif (
            box["marker"]
            and point == "after_marker"
            and Path(path).name == "manifest.json"
        ):
            kill()

    def crash_hook(found):
        orch = box.get("orch")
        if orch is None or found != point:
            return
        journal = orch.journal
        open_tx = journal.open_transaction
        if point == "before_prepared":
            hit = orch.state == "FORWARD_FINALIZE" and open_tx is None
        elif point == "after_committed":
            last = journal.events[-1]
            hit = (last.record, last.from_state, last.to_state) == (
                "committed",
                "FORWARD_FINALIZE",
                "ROBOT_HOME",
            )
        else:
            hit = (
                open_tx is not None
                and open_tx.action.kind == "recorder_seal"
                and open_tx.episode_role == "forward"
            )
        if hit:
            kill()

    w = world(
        root,
        cfg=make_config(episodes=1),
        io_hook=io_hook,
        crash_hook=crash_hook,
        scene=lambda clock: ev.FakeSceneAssessor([], clock, RUN, default=READY),
    )
    box["orch"] = w.orch
    w.orch.run()
    return 3  # the kill point was never reached


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
