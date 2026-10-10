"""A conductor in a child process for the crash tests (run as a script).

``campaign_child.py <job_dir> <world_dir> <mode>``; ``LEVI_AERI_HOME`` comes
from the environment. The conductor runs the planned campaign against the
file-backed fake world with a person who confirms everything, then writes
the report. ``mode``:

- ``count``: run to the end and print the journal's lines as JSON;
- ``kill:before:<n>`` / ``kill:after:<n>``: SIGKILL itself just before, or
  just after (synced), journal line ``n`` is written.
"""

import json
import os
import signal
import sys
from pathlib import Path

from campaign_guard import Guard
from campaign_world import FakeHost, FakeLauncher, Person, Session, World

from levi.automatic.campaign.conductor import Conductor
from levi.automatic.campaign.journal import CampaignJournal


def main(job_dir, world_dir, mode):
    Guard().install()
    if mode.startswith("kill:"):
        _, when, number = mode.split(":")
        target = int(number)
        write = CampaignJournal._write

        def killing(self, data):
            line = self.next_seq
            if when == "before" and line == target:
                os.kill(os.getpid(), signal.SIGKILL)
            write(self, data)
            if when == "after" and line == target:
                os.kill(os.getpid(), signal.SIGKILL)

        CampaignJournal._write = killing
    world = World(world_dir)
    conductor = Conductor.create(
        Path(job_dir),
        host=FakeHost(world),
        launcher=FakeLauncher(world),
        confirmations=Person(auto=True),
        session=Session(),
        reporter=lambda plan, events: "report_written",
    )
    outcome = conductor.run()
    lines = [
        [e.record, e.from_state, e.to_state, e.action.kind if e.action else None]
        for e in conductor.journal.events
    ]
    conductor.close()
    print(json.dumps({"state": outcome.state, "lines": lines}))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
