"""Crash injection (T-CP-03): a conductor in a child process is SIGKILLed
just before and just after every journal line of a whole campaign. Each
time, ``attach`` must bring the campaign to a place where a person
confirms; nothing acts until a person answers; and when a person resumes,
the campaign ends with every child run started exactly once."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from campaign_fixtures import FakePlanner, write_job
from campaign_guard import aeri_home_fixture, guard_fixture  # noqa: F401
from campaign_world import FakeHost, FakeLauncher, Person, Session, World

from levi.automatic.campaign import spec
from levi.automatic.campaign.conductor import REST_STATES, Conductor
from levi.automatic.campaign.journal import TERMINAL, CampaignJournal

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CHILD = HERE / "campaign_child.py"
# Where a recovered campaign may be: waiting for a person, asking one
# (ENV_CONFIRM), analysing (no robot) or finished.
SAFE = set(REST_STATES) | set(TERMINAL)


def setup(folder: Path):
    path = write_job(folder / "job", trials=2, segment_trials=2)
    found = spec.plan_campaign(path, job_root=folder / "jobs", planner=FakePlanner())
    return found, folder / "home", World(folder / "world")


def child(found, home, world, mode):
    env = {**os.environ, "LEVI_AERI_HOME": str(home)}
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(ROOT), env.get("PYTHONPATH")])
    )
    return subprocess.run(
        [sys.executable, str(CHILD), str(found.directory), str(world.folder), mode],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def survey(found, home, world):
    """After a kill: take the campaign over and check the rules."""
    folder = home / "campaigns" / found.spec.campaign_id
    person = Person(auto=False)
    deps = {
        "host": FakeHost(world),
        "launcher": FakeLauncher(world),
        "confirmations": person,
        "session": Session(),
        "reporter": lambda plan, events: "report_written",
    }
    if not CampaignJournal.read(folder).events:
        # Killed before the header: the campaign never began.
        conductor = Conductor.create(found.directory, home=home, **deps)
        conductor.run(max_steps=1)  # DRAFT -> PLANNED
        return conductor, person, "fresh"
    conductor = Conductor.attach(
        found.spec.campaign_id, found.directory, home=home, **deps
    )
    return conductor, person, conductor.state


def test_every_crash_point_recovers_to_a_person_and_never_starts_a_run_twice(
    tmp_path,
):
    found, home, world = setup(tmp_path / "count")
    done = child(found, home, world, "count")
    assert done.returncode == 0, done.stderr
    lines = json.loads(done.stdout)["lines"]
    assert json.loads(done.stdout)["state"] == "REPORTED"
    runs = sorted(c.run_id for c in found.children)
    assert sorted(world.launches()) == runs
    problems, seen, points = [], set(), 0
    for number in range(len(lines)):
        for when in ("before", "after"):
            point = f"kill:{when}:{number}"
            folder = tmp_path / f"{when}-{number:03d}"
            found, home, world = setup(folder)
            killed = child(found, home, world, point)
            if killed.returncode != -signal.SIGKILL:
                problems.append(
                    f"{point}: exit {killed.returncode} {killed.stderr[-300:]}"
                )
                continue
            conductor, person, state = survey(found, home, world)
            seen.add(state)
            points += 1
            try:
                if state != "fresh" and state not in SAFE:
                    problems.append(f"{point} {lines[number]}: recovered to {state}")
                    continue
                # Nothing acts while nobody answers.
                ops, launched = len(world.host_ops()), list(world.launches())
                if state != "fresh":
                    for _ in range(5):
                        conductor.advance()
                    if len(world.host_ops()) != ops or world.launches() != launched:
                        problems.append(f"{point}: acted without a person")
                # A person resumes (asking for a start only of a run that is
                # not there); the campaign ends with each run started once.
                person.auto = True
                person.decision = "relaunch"
                outcome = conductor.run()
                if conductor.state != "REPORTED":
                    problems.append(f"{point}: ended in {conductor.state} ({outcome})")
                if sorted(world.launches()) != runs:
                    problems.append(f"{point}: launches {world.launches()}")
            finally:
                conductor.close()
    assert not problems, "\n".join(problems)
    # Every line was a crash point, and the recoveries reached each kind of
    # waiting place.
    assert points == 2 * len(lines) and len(lines) > 30
    assert {"FAULT_LOCKED", "WAIT_HUMAN", "ENV_CONFIRM", "ANALYZING"} <= seen
