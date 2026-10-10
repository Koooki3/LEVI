"""A child process for the orchestrator's SIGKILL tests (run as a script).

``sm_child.py <run dir> <point> <counter file>``: run one episode on fakes
and SIGKILL this process at ``<point>`` of the home transaction
(ROBOT_HOME -> SCENE_ASSESS): before_prepared, after_prepared,
after_execute, after_acknowledged or after_committed. Each home command the
fake robot accepts appends one byte to the counter file (synced), standing
in for the command reaching the robot. No network, no robot, no GPU.
"""

import os
import signal
import sys

from aeri_harness import config, fake, rig


class CountingRobot(fake.FakeRobot):
    def __init__(self, *args, counter, **kwargs):
        super().__init__(*args, **kwargs)
        self.counter = counter

    def home(self, *, token):
        found = super().home(token=token)
        if found.executed == "yes":
            with open(self.counter, "ab") as handle:
                handle.write(b"h")
                handle.flush()
                os.fsync(handle.fileno())
        return found


def main(directory, point, counter):
    box = {}

    def hook(found):
        orch = box.get("orch")
        if orch is None or found != point:
            return
        journal = orch.journal
        open_tx = journal.open_transaction
        events = journal.events
        if point == "before_prepared":
            hit = orch.state == "ROBOT_HOME" and open_tx is None
        elif point == "after_committed":
            last = events[-1]
            hit = (last.record, last.from_state, last.to_state) == (
                "committed",
                "ROBOT_HOME",
                "SCENE_ASSESS",
            )
        else:
            hit = open_tx is not None and open_tx.action.kind == "home"
        if hit:
            os.kill(os.getpid(), signal.SIGKILL)

    clock = fake.FakeClock()
    r = rig(directory, cfg=config(episodes=1), clock=clock, create=False)
    r.robot = CountingRobot(clock, r.fence.check, counter=counter)
    from levi.automatic.orchestrator import Orchestrator

    r.orch = Orchestrator.create(r.directory, r.config, crash_hook=hook, **r.parts())
    box["orch"] = r.orch
    r.orch.run()
    return 3  # the kill point was never reached


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
