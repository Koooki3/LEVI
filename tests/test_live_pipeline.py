"""The live service end to end, without a robot or a GPU: a fake evaluation
client writes rollouts (half-written ones included), the supervisor and its
real worker process label them against a fake model server, and everything a
person or the training pool would see is checked on disk."""

import json
import os
import sqlite3
import threading
import time
import warnings
from pathlib import Path

import pytest
from conftest import scaled
from live_helpers import Rollouts

from levi.live import cli, controller, fakevlm, jsonio, mirror
from levi.live import config as live_config

NAME = "pi05_fake__stack_the_plates"


class Env:
    """A live service's whole world in a temporary directory."""

    def __init__(self, tmp_path, demo_template, **fake_options):
        self.tmp = tmp_path
        self.rollouts = Rollouts(tmp_path / "rollouts", demo_template)
        self.server, self.fake, self.port = fakevlm.serve(
            0, fakevlm.Fake(**fake_options)
        )
        c = live_config.Config()
        c.service.workspace = str(tmp_path / "ws")
        c.service.home = str(tmp_path / "home")
        c.watch.roots = [str(tmp_path / "rollouts")]
        c.watch.backlog = "process"
        c.watch.settle_s = 0.0
        c.gpu.mode = "manual"
        c.gpu.lock_file = ""
        c.vllm.port = self.port
        c.pipeline.auto_approve = True
        c.resources.threads = 1
        self.config = c
        cli.prepare(c)
        self.messages = []
        # Every supervisor and loop thread a test makes, stopped at teardown
        # whatever happened: a test that failed half-way must not leave a
        # worker process labelling (or a thread ticking) behind it.
        self.controllers, self.threads = [], []

    def controller(self, **probe):
        ports = probe.get("ports", lambda: set())
        vram = probe.get("vram", lambda: None)
        ctl = controller.Controller(
            self.config,
            # Every probe pinned: no test depends on this machine's GPU.
            probes=controller.Probes(
                vram=vram, ports=ports, holders=list, policy_vram=lambda p: None
            ),
            log=lambda *a: self.messages.append(" ".join(map(str, a))),
        )
        self.controllers.append(ctl)
        return ctl

    def start(self, ctl, **run):
        """``ctl.run(**run)`` in a thread that teardown stops and joins
        (``max_seconds`` scaled by LEVI_TEST_TIME_SCALE)."""
        if "max_seconds" in run:
            run["max_seconds"] = scaled(run["max_seconds"])
        thread = threading.Thread(target=ctl.run, kwargs=run)
        self.threads.append(thread)
        thread.start()
        return thread

    def run(self, **kwargs):
        ctl = self.controller()
        try:
            ctl.run(once=True, max_seconds=scaled(kwargs.pop("max_seconds", 300)))
        finally:
            ctl.shutdown()
        return ctl

    @property
    def ws(self):
        return Path(self.config.service.workspace)

    def sql(self, query, *args):
        db = sqlite3.connect(self.ws / "outputs/LEVI/workbench/agent/workbench.sqlite3")
        try:
            return db.execute(query, args).fetchall()
        finally:
            db.close()

    def records(self, kind):
        return [
            json.loads(body)
            for (body,) in self.sql("SELECT body FROM records WHERE kind=?", kind)
        ]

    def head_folder(self, name=NAME):
        (rows,) = self.sql("SELECT revision FROM heads WHERE dataset=?", name)
        return (
            self.ws
            / f"outputs/LEVI/workbench/agent/datasets/{name}/revisions/{rows[0]}"
        )

    def atoms(self, episode, name=NAME):
        path = self.head_folder(name) / f"annotations/episode_{episode:06d}.json"
        return json.loads(path.read_text())["atoms"]

    def state(self, name=NAME):
        return mirror.load_state(self.config, name)

    def close(self):
        controllers, threads = self.controllers, self.threads
        self.controllers, self.threads = [], []
        try:
            for ctl in controllers:
                ctl.running = False
                ctl.wake.set()
            for thread in threads:
                thread.join(scaled(60))
                if thread.is_alive():
                    warnings.warn(
                        f"a live supervisor thread outlived teardown: {thread}"
                    )
            for ctl in controllers:
                # Stops its worker (by process group) if one is left; one that
                # fails must not keep the others, or the fake server, running.
                try:
                    ctl.shutdown()
                except Exception as error:  # noqa: BLE001 (teardown goes on)
                    warnings.warn(f"a live supervisor did not shut down: {error!r}")
        finally:
            self.server.shutdown()


@pytest.fixture
def env(tmp_path, demo_template):
    made = []

    def make(**fake_options):
        made.append(Env(tmp_path, demo_template, **fake_options))
        return made[-1]

    yield make
    for item in made:
        item.close()


def test_a_batch_is_labelled_without_a_person_and_without_an_outcome_label(env):
    e = env()
    e.rollouts.write(0)
    e.rollouts.write(1)
    e.rollouts.begin(2)  # still being written
    e.rollouts.write(3)
    e.rollouts.incomplete(3, "fr3_fault")  # aborted by an FR3 fault
    e.rollouts.write(4)
    e.rollouts.discard(4)
    ctl = e.run()

    state = e.state()
    assert {d: r["state"] for d, r in state["demos"].items()} == {
        "demo_0000": "done",
        "demo_0001": "done",
    }
    capture = Path(state["capture"])
    assert sorted(p.name for p in capture.glob("demo_*")) == ["demo_0000", "demo_0001"]
    # Segments are committed, stamped "auto", each with its subtask and outcome.
    for episode in (0, 1):
        atoms = e.atoms(episode)
        assert len(atoms) == 5 and all(a["style"] == "subtask" for a in atoms)
        assert {a["levi"]["review"] for a in atoms} == {"auto"}
        assert all(a["levi"]["origin"]["review"] == "auto" for a in atoms)
        assert {a["levi"]["outcome"] for a in atoms} == {"success"}
    # The change was approved by the automatic approver, and says so.
    committed = [c for c in e.records("changes") if c["status"] == "committed"]
    assert len(committed) == 1
    assert committed[0]["provenance"]["reviewer_type"] == "auto"
    assert committed[0]["provenance"]["reviewer"] == "live-auto"
    # No human outcome label anywhere: the verdict is an anchored-review record.
    assert not list(e.ws.rglob("outcomes"))
    assert not any(
        p["kind"] == "outcome"
        for c in e.records("changes")
        for p in c["proposals"]
        if c["status"] == "committed"
    )
    verdict = state["demos"]["demo_0000"]["verdict"]
    assert (
        verdict["outcome"] == "success"
        and verdict["review"] == "auto"
        and verdict["evaluated"] is False
    )
    anchored = [
        r for r in e.records("runs") if r["context"]["workflow"].get("anchored")
    ]
    assert len(anchored) == 1 and anchored[0]["status"] == "waiting_for_review"
    # Everything the approver did is in its audit log, nothing was refused.
    lines = [
        json.loads(x) for x in (e.ws / "live/audit.jsonl").read_text().splitlines()
    ]
    tools = [x["tool"] for x in lines]
    assert tools.count("plans.approve") == 4 and tools.count("changes.commit") == 2
    # Each state-changing call is logged when allowed and again with its outcome.
    assert {x["decision"] for x in lines} == {"allowed", "completed"} and {
        x["principal"] for x in lines
    } == {"live-auto"}
    # What the page shows.
    status = ctl.status()
    row = status["datasets"][NAME]
    assert (
        row["done"],
        row["pending"],
        row["waiting"],
        row["incomplete"],
        row["fr3_fault"],
    ) == (2, 0, 1, 1, 1)
    assert row["fault"] and "FR3" in row["fault_reasons"][0]
    # The source was only read.
    assert not list(e.rollouts.root.rglob(".partial-*"))


def test_the_task_instruction_reaches_the_model_and_the_spec(env):
    e = env()
    e.rollouts.write(0)
    e.run()
    prompts = []
    for payload in e.fake.calls:
        content = payload["messages"][-1]["content"]
        prompts.append(
            content
            if isinstance(content, str)
            else " ".join(p.get("text", "") for p in content)
        )
    assert any(e.rollouts.text in p for p in prompts)
    assert (
        sum(e.rollouts.text in p for p in prompts) >= 2
    )  # temporal and release review
    run = next(r for r in e.records("runs") if r["context"]["workflow"].get("anchored"))
    spec = run["context"]["workflow"]["anchored"]
    assert e.rollouts.text in spec["question"] and "{task}" not in spec["question"]
    assert spec["status"] == "candidate"


def test_a_person_s_annotation_is_never_annotated_over(env):
    e = env()
    e.rollouts.write(0)
    e.rollouts.write(1)
    human = [
        {
            "role": "user",
            "content": "my label",
            "style": "subtask",
            "timestamp": 0.5,
            "to": 1.5,
        }
    ]
    folder = e.ws / f"outputs/LEVI/workbench/annotations/{NAME}"
    folder.mkdir(parents=True)
    (folder / "episode_000001.json").write_text(
        json.dumps({"episode_index": 1, "atoms": human})
    )
    e.run()
    state = e.state()
    assert state["demos"]["demo_0001"]["state"] == "skipped_human"
    assert state["demos"]["demo_0000"]["state"] == "done"
    assert e.atoms(1) == human  # carried through the commit untouched
    assert all(a["levi"]["review"] == "auto" for a in e.atoms(0))


def test_a_demo_finishing_late_renumbers_episodes_without_losing_labels(env):
    e = env()
    e.rollouts.write(5)
    e.run()
    assert {a["levi"]["origin"]["run_id"] for a in e.atoms(0)}  # demo_0005 is episode 0
    first = {a["content"] for a in e.atoms(0)}
    # demo_0003 finishes after demo_0005: the view renumbers (0003 -> 0, 0005 -> 1).
    e.rollouts.write(3)
    e.run()
    state = e.state()
    assert {d: r["state"] for d, r in state["demos"].items()} == {
        "demo_0003": "done",
        "demo_0005": "done",
    }
    assert state["demos"]["demo_0003"]["episode_index"] == 0
    assert len(e.atoms(0)) == 5 and len(e.atoms(1)) == 5
    assert {a["content"] for a in e.atoms(1)} == first
    # One batch per task at a time, nothing labelled twice.
    committed = [c for c in e.records("changes") if c["status"] == "committed"]
    assert len(committed) == 2


def test_restarting_continues_where_it_stopped_and_repeats_nothing(env):
    e = env(delay=0.4)
    for n in range(3):
        e.rollouts.write(n)
    ctl = e.controller()
    thread = e.start(ctl, once=True, max_seconds=120)
    deadline = time.time() + scaled(60)
    while time.time() < deadline:
        progress = jsonio.read(e.ws / "live/worker.json") or {}
        if progress.get("phase") in ("temporal", "anchored") and e.fake.calls:
            break
        time.sleep(0.1)
    ctl.preempt("session", "test: the supervisor is killed mid-batch")
    ctl.running = False
    ctl.wake.set()
    thread.join(scaled(30))
    assert e.state()["current"] is not None  # the batch is remembered
    calls_before = len(e.fake.calls)
    e.fake.delay = 0
    e.run()
    state = e.state()
    assert {r["state"] for r in state["demos"].values()} == {"done"} and state[
        "current"
    ] is None
    committed = [c for c in e.records("changes") if c["status"] == "committed"]
    assert (
        len(committed) == 1
        and len({p["episode_index"] for p in committed[0]["proposals"]}) == 3
    )
    # A third start finds nothing to do and asks the model nothing.
    calls = len(e.fake.calls)
    assert calls >= calls_before
    e.run()
    assert len(e.fake.calls) == calls


def test_two_tasks_growing_together_are_handled_one_at_a_time(
    env, tmp_path, demo_template
):
    e = env()
    other = Rollouts(
        e.rollouts.root, demo_template, task="put_the_cup_away", text="put the cup away"
    )
    for n in range(2):
        e.rollouts.write(n)
        other.write(n)
    e.run()
    started = [
        m for m in e.messages if m.startswith(("batch started", "batch finished"))
    ]
    assert len(started) == 4
    # started A, finished A, started B, finished B: never overlapping.
    assert [m.split()[1] for m in started] == [
        "started",
        "finished",
        "started",
        "finished",
    ]
    assert {e.state(n)["name"] for n in (NAME, "pi05_fake__put_the_cup_away")}
    for name in (NAME, "pi05_fake__put_the_cup_away"):
        assert {r["state"] for r in e.state(name)["demos"].values()} == {"done"}


def test_without_the_approver_the_plan_waits_for_a_person(env):
    e = env()
    e.config.pipeline.auto_approve = False
    e.rollouts.write(0)
    ctl = e.run()
    state = e.state()
    assert state["current"] and state["demos"]["demo_0000"]["state"] == "mirrored"
    (run,) = e.records("runs")
    assert run["status"] == "planned" and not run["plan"].get("approval")
    assert (
        NAME in ctl.awaiting
        and ctl.status()["datasets"][NAME]["state"] == "awaiting_approval"
    )
    assert not [c for c in e.records("changes") if c["status"] == "committed"]
    assert not (e.ws / "live/audit.jsonl").exists()


def test_a_model_server_that_fails_is_retried_not_trusted(env):
    """The first two requests fail with a server error: the batch still ends
    with the episode annotated once, by the answers that did come, and the
    failures cost requests, not a label."""
    e = env(fail=lambda n, payload: n <= 2)
    e.rollouts.write(0)
    e.run()
    row = e.state()["demos"]["demo_0000"]
    assert row["state"] == "done" and row["attempts"] == 0
    assert len(e.atoms(0)) == 5
    assert len(e.fake.calls) >= 3  # the two that failed were asked again
    assert len([c for c in e.records("changes") if c["status"] == "committed"]) == 1


def test_a_second_pass_over_a_quiet_dataset_does_no_work(env):
    e = env()
    e.rollouts.write(0)
    e.run()
    calls = len(e.fake.calls)
    e.run()
    assert len(e.fake.calls) == calls
    assert len([c for c in e.records("changes") if c["status"] == "committed"]) == 1


def test_a_worker_killed_without_warning_does_not_hold_up_the_next_one(env):
    import signal

    e = env(delay=0.4)
    for n in range(3):
        e.rollouts.write(n)
    ctl = e.controller()
    thread = e.start(ctl, once=True, max_seconds=120)
    deadline = time.time() + scaled(60)
    while time.time() < deadline and not (ctl.worker and e.fake.calls):
        time.sleep(0.1)
    os.killpg(ctl.worker.pid, signal.SIGKILL)  # no pause, leases left behind
    thread.join(scaled(60))
    e.fake.delay = 0
    started = time.time()
    e.run()
    assert time.time() - started < 100  # not the 3 minutes a lease takes to expire
    assert {r["state"] for r in e.state()["demos"].values()} == {"done"}


def test_teardown_stops_a_batch_a_test_left_running(env):
    """A test that fails while a batch runs leaves no worker process behind:
    the environment stops its supervisors and their workers."""
    e = env(delay=5.0)
    e.rollouts.write(0)
    ctl = e.controller()
    thread = e.start(ctl, once=True, max_seconds=240)
    deadline = time.time() + scaled(60)
    while time.time() < deadline and not (ctl.worker and e.fake.calls):
        time.sleep(0.1)
    worker = ctl.worker
    assert worker is not None and worker.poll() is None
    e.close()  # what the fixture does after a failed assertion
    assert not thread.is_alive()
    assert worker.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(worker.pid, 0)  # the whole process group is gone


def test_teardown_goes_on_when_a_supervisor_fails_to_shut_down(env):
    e = env()
    first, second = e.controller(), e.controller()
    stopped = []

    def broken():
        raise RuntimeError("shutdown failed")

    first.shutdown = broken
    second.shutdown = lambda: stopped.append("second")
    shutdown = e.server.shutdown
    e.server.shutdown = lambda: (stopped.append("server"), shutdown())
    with pytest.warns(UserWarning, match="did not shut down"):
        e.close()
    assert stopped == ["second", "server"]


def test_a_new_episode_cancels_the_request_in_flight_and_the_batch_resumes_after(env):
    """Timeshare beside a policy server: the model works between episodes, a
    new episode stops its request at once, the finished episodes stay done and
    the rest continues when the policy stops inferring."""
    e = env(delay=3.0)
    e.config.gpu.mode = "timeshare"
    e.config.vllm.adopt_external = True  # the fake server stands in for vLLM
    for n in range(2):
        e.rollouts.write(n)
    e.rollouts.session("homing")
    ctl = e.controller(ports=lambda: {8000})  # a policy server is up
    thread = e.start(ctl, max_seconds=240)
    try:
        progress = lambda: jsonio.read(e.ws / "live/worker.json") or {}
        deadline = time.time() + scaled(60)
        while time.time() < deadline and not (
            e.fake.started and progress().get("phase") in ("temporal", "anchored")
        ):
            time.sleep(0.1)
        assert e.fake.started, "the model never got a request while the policy was idle"
        flipped = time.time()
        e.rollouts.session("running")  # a new episode begins: the policy infers
        deadline = time.time() + scaled(30)
        while time.time() < deadline and progress().get("phase") != "gated":
            time.sleep(0.1)
        assert progress().get("phase") == "gated", (
            e.messages,
            (e.ws / "live/logs/worker.log").read_text()[-1500:],
        )
        (run,) = [
            r
            for r in e.records("runs")
            if r["context"]["workflow"]["kind"] == "temporal"
        ]
        assert run["status"] == "paused"
        events = [
            json.loads(b)
            for (b,) in e.sql("SELECT body FROM events WHERE run_id=?", run["id"])
        ]
        assert "model_request_stopped" in [x["type"] for x in events]
        # The in-flight request was cut within a second of `running` appearing.
        cut = next(x["time"] for x in events if x["type"] == "model_request_stopped")
        assert 0 <= cut - flipped <= 1.0, (
            f"cancelled {cut - flipped:.2f} s after running"
        )
        sent = e.fake.started
        time.sleep(3)
        assert e.fake.started == sent  # nothing goes out while the policy infers
        assert not [c for c in e.records("changes") if c["status"] == "committed"]
        e.rollouts.session("finished")  # the evaluation ends
        deadline = time.time() + scaled(120)
        while time.time() < deadline:
            state = e.state() or {}
            if state.get("demos") and all(
                r.get("state") == "done" for r in state["demos"].values()
            ):
                break
            time.sleep(0.5)
        assert {r["state"] for r in e.state()["demos"].values()} == {"done"}
        committed = [c for c in e.records("changes") if c["status"] == "committed"]
        assert (
            len(committed) == 1
            and len({p["episode_index"] for p in committed[0]["proposals"]}) == 2
        )
    finally:
        ctl.running = False
        ctl.wake.set()
        thread.join(scaled(60))
        ctl.shutdown()


HUMAN = """
import sys
from levi import paths
paths.configure()
from levi.agent.capabilities import invoke
from levi.agent.runtime import Workbench
from levi.agent.security import Principal

wb = Workbench(paths.STATE)
human = Principal("a-person", human=True)
mode, run_id = sys.argv[1], sys.argv[2]
run = wb.store.get("runs", run_id)
if mode == "plan":
    invoke(wb, human, "plans.approve", {"run_id": run_id, "revision": run["plan"]["revision"]})
else:
    change = wb.store.get("changes", run["changes"])
    invoke(wb, human, "changes.approve", {"changeset_id": change["id"], "revision": change["revision"]})
    change = wb.store.get("changes", change["id"])
    invoke(wb, human, "changes.commit", {"changeset_id": change["id"], "revision": change["revision"]}, key="person:" + change["id"])
"""


def as_a_person(e, mode, run_id):
    import subprocess
    import sys

    from levi.live import resources

    done = subprocess.run(
        [sys.executable, "-c", HUMAN, mode, run_id],
        env=resources.service_env(e.config),
        cwd=Path(controller.__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-800:]


def test_waiting_for_a_person_does_not_loop_and_a_person_s_commit_is_not_a_failure(env):
    """Without the approver: the plan waits for a person; after the plan is
    approved the draft waits for a person; the supervisor starts no worker for
    either wait, and what the person commits is recorded as done (review
    human), not as a failed attempt."""
    e = env()
    e.config.pipeline.auto_approve = False
    e.rollouts.write(0)
    started = lambda ctl: sum(m.startswith("batch started") for m in e.messages)
    ctl = e.controller()
    ctl.run(once=True, max_seconds=scaled(120))
    assert ctl.awaiting[NAME]["kind"] == "plan"
    assert started(ctl) == 1
    (run,) = e.records("runs")
    # The wait is on disk too: a restarted supervisor does not have to start a
    # model and a worker just to find out.
    saved = e.state()["awaiting"]
    assert saved["kind"] == "plan" and saved["run_id"] == run["id"]
    as_a_person(e, "plan", run["id"])
    # Time passes: the plan is approved, so exactly one more worker runs it ...
    ctl.awaiting[NAME]["at"] -= 1000
    deadline = time.time() + scaled(120)
    while time.time() < deadline and started(ctl) < 2:
        ctl.tick()
        time.sleep(0.3)
    while time.time() < deadline and ctl.worker is not None:
        ctl.tick()
        time.sleep(0.3)
    ctl.tick()
    assert ctl.awaiting[NAME]["kind"] == "changes", (ctl.awaiting, e.messages[-8:])
    assert e.state()["awaiting"]["kind"] == "changes"
    # ... and then nothing starts while the draft waits, however long it is.
    for _ in range(6):
        ctl.awaiting[NAME]["at"] -= 1000
        ctl.tick()
        time.sleep(0.2)
    assert started(ctl) == 2 and ctl.worker is None
    assert ctl.status()["datasets"][NAME]["state"] == "awaiting_approval"
    assert ctl.vllm.state == "stopped" or not ctl.vllm.mine()
    (run,) = [
        r for r in e.records("runs") if r["context"]["workflow"]["kind"] == "temporal"
    ]
    as_a_person(e, "changes", run["id"])

    # What the person committed is this batch's work, recorded as done by a
    # person -- not a failed attempt. The release review (a second run) then
    # waits for its own plan to be approved.
    def settle(kind):
        ctl.awaiting.get(NAME, {}).update(at=0.0)
        deadline = time.time() + scaled(150)
        while time.time() < deadline:
            ctl.tick()
            if ctl.worker is None and ctl.awaiting.get(NAME, {}).get("kind") == kind:
                return
            time.sleep(0.4)
        raise AssertionError(("no wait for", kind, ctl.awaiting, e.messages[-5:]))

    settle("plan")
    row = e.state()["demos"]["demo_0000"]
    assert row["state"] == "annotating" and row["attempts"] == 0 and "reason" not in row
    assert row["temporal"]["review"] == "human" and row["temporal"]["segments"] == 5
    anchored = [
        r for r in e.records("runs") if r["context"]["workflow"].get("anchored")
    ]
    as_a_person(e, "plan", anchored[0]["id"])
    ctl.awaiting[NAME]["at"] = 0.0
    deadline = time.time() + scaled(150)
    while time.time() < deadline:
        ctl.tick()
        if ctl.worker is None and e.state()["demos"]["demo_0000"]["state"] == "done":
            break
        time.sleep(0.4)
    row = e.state()["demos"]["demo_0000"]
    assert row["state"] == "done" and row["verdict"]["review"] == "auto"
    ctl.shutdown()


def test_a_worker_finding_a_person_s_wait_needs_no_model(env):
    """With the approver off a plan that is not approved yet is a wait for a
    person: the worker finds that out without the model server (down here),
    exits 11 and not 10 ("waiting for the model server")."""
    e = env()
    e.config.pipeline.auto_approve = False
    e.rollouts.write(0)
    ctl = e.controller()
    ctl.run(once=True, max_seconds=scaled(120))
    assert ctl.awaiting[NAME]["kind"] == "plan"
    ctl.shutdown()
    # The supervisor restarts with the model server gone.
    e.config.vllm.port = free_port()
    ctl = e.controller()
    assert ctl.awaiting[NAME]["kind"] == "plan"  # read back from the dataset state
    ctl.awaiting.clear()  # ... and suppose it had not: the worker must cope
    e.messages.clear()
    ctl._spawn(NAME)  # (the supervisor itself would not: no model is up)
    deadline = time.time() + scaled(120)
    while time.time() < deadline and ctl.worker is not None:
        ctl.tick()
        time.sleep(0.3)
    assert ctl.awaiting[NAME]["kind"] == "plan"
    assert not any("waiting for the model server" in m for m in e.messages)
    ctl.shutdown()


def free_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_open_review_runs_are_kept_up_to_a_limit_and_older_ones_archived(env):
    """Every batch leaves its release review open (waiting_for_review) for a
    person to accept; they must not pile up for ever. The newest keep_review_runs
    keep their frozen input (committing one needs it); older ones are cancelled
    and cleaned, their verdicts staying in the dataset state."""
    e = env()
    e.config.pipeline.keep_review_runs = 1
    for n in range(3):
        e.rollouts.write(n)
        e.run()
    reviews = [r for r in e.records("runs") if r["context"]["workflow"].get("anchored")]
    assert len(reviews) == 3
    open_runs = [r for r in reviews if r["status"] == "waiting_for_review"]
    assert len(open_runs) == 1 and sum(r["status"] == "cancelled" for r in reviews) == 2
    state = e.state()
    assert state["review_runs_open"] == 1 and state["review_runs"] == [
        open_runs[0]["id"]
    ]
    base = e.ws / f"outputs/LEVI/workbench/agent/datasets/{NAME}/runs"
    assert (base / open_runs[0]["id"] / "input").is_dir()  # still committable
    for r in reviews:
        if r["status"] == "cancelled":
            assert not (base / r["id"] / "input").exists()
    # The verdicts of all three episodes are still there.
    assert {d["verdict"]["outcome"] for d in state["demos"].values()} == {"success"}
    assert len(state["demos"]) == 3
    ctl = e.controller()
    ctl._refresh(time.time(), True)
    assert ctl.status()["datasets"][NAME]["review_runs_open"] == 1


def rewrite_gripper(e, n, commands):
    """A demo whose gripper command is ``commands`` (one per frame)."""
    import pandas as pd

    demo = e.rollouts.begin(n)
    rows = pd.read_csv(demo / "gripper_state.csv")
    rows["last_gripper_command"] = commands
    rows.to_csv(demo / "gripper_state.csv", index=False)
    e.rollouts.finish(n)


def test_the_terminal_aware_review_judges_by_what_the_episode_ends_on(env):
    """generic-release.v2, named in the configuration: a release that is
    followed by a grasp is a failure whatever the model said about it, one that
    is not is a success, and the default rule still calls both a success."""
    from levi.live import api

    frames = 30
    e = env()
    e.config.pipeline.anchored_spec = "generic-release.v2.json"
    # The template: close, open at frame 18 (the release), close at 24 again.
    e.rollouts.write(0)
    # Closed, released at 18, open until the end.
    rewrite_gripper(e, 1, ["open"] * 6 + ["close"] * 12 + ["open"] * (frames - 18))
    e.run()
    state = e.state()
    again, held = (state["demos"][f"demo_000{n}"]["verdict"] for n in (0, 1))
    assert again["outcome"] == "failure" and again["undecided"] is False
    assert again["valid_events"] == 1 and again["events"] == 1
    assert again["rule"] == "last_valid_not_regrasped"
    assert again["closes_after_last_valid"] == 1
    assert again["place_outcome"] == "success"
    assert again["basis"]["last_valid_frame"] == 18
    assert held["spec_version"] == again["spec_version"] == 2
    assert held["min_valid"] == 1
    assert held["outcome"] == "success" and held["undecided"] is False
    assert held["closes_after_last_valid"] == 0 and held["place_outcome"] == "success"
    assert held["basis"]["require_place"] is True
    assert "missing_inputs" not in held["basis"]
    # The review record keeps the closes the rule read.
    records = {
        r["episode_index"]: r
        for r in e.records("anchored")
        if r["spec"]["version"] == 2
    }
    assert records[0]["closes"] == [6, 24] and records[1]["closes"] == [6]
    # The review's own outcome proposal says the place condition is the live
    # verdict's to apply.
    proposals = [
        p
        for c in e.records("changes")
        for p in c["proposals"]
        if p["kind"] == "outcome"
    ]
    assert proposals and all(
        "last-placement condition is not applied" in p["uncertainty"] for p in proposals
    )
    assert all(r["basis"]["missing_inputs"] == ["place"] for r in records.values())
    # The statistics records and a backfill carry the rule and the place
    # outcome, and the aggregates still count the verdicts.
    from levi.live import backfill, stats

    rows = {r["demo"]: r for r in stats.read(e.ws / "live")}
    got = rows["demo_0000"]["result"]["verdict"]
    assert got["outcome"] == "failure" and got["rule"] == again["rule"]
    assert got["place_outcome"] == "success"
    assert rows["demo_0000"]["result"]["spec"]["release_review_version"] == 2
    summary = stats.summarize(list(rows.values()))
    assert summary["outcome"]["verdicts"] == {"failure": 1, "success": 1}
    (e.ws / "live/stats.jsonl").unlink()
    rebuilt = {i["demo"]: i["record"] for i in backfill.plan(e.config)}
    assert rebuilt["demo_0000"]["result"]["verdict"] == got
    assert rebuilt["demo_0000"]["result"]["spec"]["release_review_version"] == 2
    # The page's rows carry the new fields.
    row = api._demo_row("demo_0000", state["demos"]["demo_0000"])["verdict"]
    assert row["rule"] == "last_valid_not_regrasped"
    assert row["place_outcome"] == "success" and row["closes_after_last_valid"] == 1


def test_the_default_review_is_the_same_with_the_terminal_rule_available(env):
    from levi.live import api

    e = env()
    e.rollouts.write(0)
    e.run()
    verdict = e.state()["demos"]["demo_0000"]["verdict"]
    assert verdict["outcome"] == "success"
    assert not {"rule", "place_outcome", "closes_after_last_valid"} & set(verdict)
    assert set(verdict["basis"]) == {"valid_events", "min_valid"}
    assert all(r["spec"]["version"] == 1 for r in e.records("anchored"))
    assert "closes" not in e.records("anchored")[0]
    row = api._demo_row("demo_0000", e.state()["demos"]["demo_0000"])["verdict"]
    assert row["rule"] is None and row["place_outcome"] is None
    assert row["spec_version"] == 1 and row["min_valid"] is None
    # No placement doubt on a default-rule proposal.
    assert not any(
        "last-placement" in p["uncertainty"]
        for c in e.records("changes")
        for p in c["proposals"]
        if p["kind"] == "outcome"
    )


def test_a_demo_without_committed_time_segments_is_judged_on_the_gripper_alone():
    """No time segments to read (the temporal step failed for the demo, or its
    row belongs to another episode number): ``missing``, which the rule reads
    as a missing input, never as a success."""
    from levi.live.worker import Worker

    class Store:
        def get(self, kind, key):
            if key != "cs1":
                raise KeyError(key)
            place = {
                "kind": "segment",
                "episode_index": 4,
                "style": "subtask",
                "subtask_id": "place",
                "outcome": "failure",
            }
            return {
                "proposals": [
                    {**place, "start": 3.0},
                    # The last place, rejected by the person who reviewed it.
                    {**place, "start": 6.0, "outcome": "success"},
                ],
                "decisions": {"1": "rejected"},
            }

    worker = Worker.__new__(Worker)
    worker.store = Store()
    row = {"temporal": {"changeset": "cs1"}, "episode_index": 4}
    # The rejected, later place does not count: the earlier failure stands.
    assert worker.place_of(row, 4) == "failure"
    assert worker.place_of(row, 5) == "missing"  # not this episode's row
    assert worker.place_of(None, 4) == "missing"
    assert worker.place_of({"temporal": {}, "episode_index": 4}, 4) == "missing"
    assert (
        worker.place_of({"temporal": {"changeset": "gone"}, "episode_index": 4}, 4)
        == "missing"
    )


# --- review only (pipeline.temporal = false) and the operator label ---------------


def review_only(e):
    e.config.pipeline.temporal = False
    e.config.pipeline.anchored_spec = "generic-release.v3.json"
    e.config.validate()


def label(e, n, **fields):
    """The operator's label in a finished demo's ``metadata.json`` (what the
    evaluation client writes at the key press)."""
    path = e.rollouts.demo(n) / "metadata.json"
    meta = json.loads(path.read_text())
    meta["eval"].update(fields)
    path.write_text(json.dumps(meta))


def anchored_runs(e):
    return [r for r in e.records("runs") if r["context"]["workflow"].get("anchored")]


def temporal_runs(e):
    return [
        r for r in e.records("runs") if not r["context"]["workflow"].get("anchored")
    ]


def test_review_only_labels_without_time_segments(env):
    from levi.live import api, stats

    frames = 30
    e = env()
    review_only(e)
    e.rollouts.write(0)  # released at 18, closed again at 24
    rewrite_gripper(e, 1, ["open"] * 6 + ["close"] * 12 + ["open"] * (frames - 18))
    e.run()
    assert temporal_runs(e) == [] and len(anchored_runs(e)) == 1
    state = e.state()
    for demo, episode in (("demo_0000", 0), ("demo_0001", 1)):
        row = state["demos"][demo]
        assert row["state"] == "done" and row["episode_index"] == episode
        assert "temporal" not in row and row["verdict"]["spec_version"] == 3
    assert state["demos"]["demo_0001"]["verdict"]["outcome"] == "success"
    assert state["demos"]["demo_0001"]["verdict"]["undecided"] is False
    # No vocabulary, no annotation, no outcome label: nothing was committed.
    outputs = e.ws / "outputs"
    assert not list(outputs.rglob("vocabulary.json"))
    assert not [p for p in outputs.rglob("episode_*.json") if "annotations" in p.parts]
    assert not list(e.ws.rglob("outcomes"))
    assert not [c for c in e.records("changes") if c["status"] == "committed"]
    # The statistics say what ran: no segments, no guideline, version 3.
    rows = {r["demo"]: r for r in stats.read(e.ws / "live")}
    result = rows["demo_0001"]["result"]
    assert result["segments"] is None and result["segment_labels"] is None
    assert result["spec"]["guideline"] is None
    assert result["spec"]["release_review"] == "generic-release"
    assert result["spec"]["release_review_version"] == 3
    assert set(result["spec"]["sha256"]) == {"generic-release.v3.json"}
    assert rows["demo_0001"]["timeline"]["to_commit_s"] is None
    assert rows["demo_0001"]["timeline"]["to_verdict_s"] is not None
    # The log line leaves out the segment count.
    log = (e.ws / "live/logs/worker.log").read_text()
    lines = [x for x in log.splitlines() if "episode demo=demo_0001" in x]
    assert lines and "segments=" not in lines[-1] and "verdict=success" in lines[-1]
    # A second pass does no work: nothing loops back to "mirrored".
    calls = len(e.fake.calls)
    e.run()
    assert len(e.fake.calls) == calls
    page = api._demo_row("demo_0001", e.state()["demos"]["demo_0001"])
    assert page["segments"] is None and page["verdict"]["outcome"] == "success"


def test_review_only_regrasp_is_a_failure(env):
    """Version 3 is version 2's gripper rule without the place condition: a
    release followed by a grasp is a failure, and nothing reads a place
    time segment."""
    e = env()
    review_only(e)
    e.rollouts.write(0)  # released at 18, closed again at 24
    e.run()
    verdict = e.state()["demos"]["demo_0000"]["verdict"]
    assert verdict["outcome"] == "failure" and verdict["undecided"] is False
    assert verdict["rule"] == "last_valid_not_regrasped"
    assert verdict["closes_after_last_valid"] == 1 and verdict["valid_events"] == 1
    assert "place_outcome" not in verdict
    assert verdict["basis"].get("require_place") in (None, False)
    assert "missing_inputs" not in verdict["basis"]
    (record,) = e.records("anchored")
    assert record["closes"] == [6, 24] and record["spec"]["version"] == 3


def test_review_only_judges_an_episode_a_person_annotated(env):
    """The review writes no annotation, so a person's time segments are never
    written over: their episode is judged like the others, and their file is
    left as it was."""
    e = env()
    review_only(e)
    e.rollouts.write(0)
    e.rollouts.write(1)
    human = [
        {
            "role": "user",
            "content": "my label",
            "style": "subtask",
            "timestamp": 0.5,
            "to": 1.5,
        }
    ]
    folder = e.ws / f"outputs/LEVI/workbench/annotations/{NAME}"
    folder.mkdir(parents=True)
    path = folder / "episode_000001.json"
    path.write_text(json.dumps({"episode_index": 1, "atoms": human}))
    before = path.read_bytes()
    e.run()
    state = e.state()
    assert {d: r["state"] for d, r in state["demos"].items()} == {
        "demo_0000": "done",
        "demo_0001": "done",
    }
    assert state["demos"]["demo_0001"]["verdict"]["outcome"] == "failure"
    assert path.read_bytes() == before
    assert not (folder / "vocabulary.json").exists()


class _ReviewOnlyWorker:
    """``Worker.anchored`` and ``Worker.finish`` against a real state file, with
    the store and the model replaced: the review run 'ends' and has no record
    for the episode (``anchored.get`` raises KeyError)."""

    def __init__(self, config):
        from levi.live.worker import Worker

        self.worker = Worker.__new__(Worker)
        w = self.worker
        w.config, w.name = config, NAME
        w.progress_path = config.live_dir / "worker.json"
        w.detail, w._beat, w.stopping = {}, 0.0, False
        w.repo_id, w.view, w.walls, w.gated, w.lengths = "local/x", None, {}, [0, 0], {}
        w.provider_spec = {"name": "fake"}
        w.context = lambda *a: {"fake": True}
        w.plan = lambda context: "run-1"
        w.drive = lambda run_id, what: {"id": run_id, "status": "waiting_for_review"}
        w.run_state = lambda run_id: {"id": run_id, "status": "waiting_for_review"}

        def call(name, arguments, key=None, principal=None):
            if name == "anchored.get":
                raise KeyError(arguments["episode"])
            raise AssertionError(name)

        w.call = call

    def batch(self):
        state = mirror.load_state(self.worker.config, NAME)
        demos = mirror.waiting_demos(state, self.worker.config.pipeline.max_attempts)
        if not demos:
            return None
        batch = {"demos": demos, "temporal": {}, "anchored": None, "done": []}
        index = {d: 3 for d in demos}
        self.worker.anchored(batch, index)
        self.worker.finish(batch, index)
        return batch


def test_review_only_missing_verdict_retries_then_fails(tmp_path):
    c = live_config.Config()
    c.service.workspace = str(tmp_path / "ws")
    c.service.home = str(tmp_path / "home")
    c.pipeline.cleanup = False
    c.pipeline.max_attempts = 2
    review_only(type("E", (), {"config": c})())
    jsonio.write(
        mirror.state_path(c, NAME),
        {
            "name": NAME,
            "task_text": "put the eggplant on the plate",
            "demos": {"demo_0000": {"state": "mirrored", "attempts": 0}},
        },
    )
    probe = _ReviewOnlyWorker(c)
    batches = 0
    while probe.batch() is not None:
        batches += 1
        assert batches <= c.pipeline.max_attempts, "the batch would loop forever"
    row = mirror.load_state(c, NAME)["demos"]["demo_0000"]
    assert batches == 2
    assert row["state"] == "failed" and row["attempts"] == 2
    assert row["episode_index"] == 3 and row["verdict"] is None
    assert row["reason"].startswith("no release-review verdict: ")
    # Nothing is left to label: the worker's next batch is empty (NOTHING).
    assert mirror.waiting_demos(mirror.load_state(c, NAME), 2) == []


def test_with_time_segments_on_a_missing_verdict_still_waits_for_them(tmp_path):
    """The default pipeline: a missing review verdict does not count an attempt
    (the temporal run decides), exactly as before."""
    c = live_config.Config()
    c.service.workspace = str(tmp_path / "ws")
    c.service.home = str(tmp_path / "home")
    c.pipeline.cleanup = False
    jsonio.write(
        mirror.state_path(c, NAME),
        {
            "name": NAME,
            "task_text": "x",
            "demos": {"demo_0000": {"state": "mirrored", "attempts": 0}},
        },
    )
    _ReviewOnlyWorker(c).batch()
    row = mirror.load_state(c, NAME)["demos"]["demo_0000"]
    assert row["state"] == "mirrored" and row["attempts"] == 0
    assert "episode_index" not in row and row["verdict"] is None


def test_operator_label_is_kept_apart_and_never_reaches_the_model(env):
    """A dual-label demo: the operator said success, the agent (the fake model
    sees the object land elsewhere) says failure. Both are kept, apart; the
    model was asked the spec's question and nothing of the operator's label."""
    from levi.live import api, generic, stats

    marker = "OPERATOR-MARKER-7f3a"
    e = env(answers={"landed": "elsewhere"})
    review_only(e)
    e.rollouts.write(0)
    label(
        e,
        0,
        outcome="success",
        verdict_by="operator",
        operator_outcome="success",
        operator_labelled_at=marker,
        eval_note=marker,
        label_mode="dual",
    )
    e.run()
    row = e.state()["demos"]["demo_0000"]
    assert row["operator_label"] == {
        "outcome": "success",
        "by": "operator",
        "source": "capture-metadata",
    }
    assert row["outcome_recorded"] == "success"
    assert row["verdict"]["outcome"] == "failure"
    assert "operator" not in json.dumps(row["verdict"])
    # What the model was asked: the spec's question, never the label.
    question = generic.anchored_spec(e.rollouts.text, "generic-release.v3.json")[
        "question"
    ]
    asked = [
        payload for payload in e.fake.calls if payload.get("max_completion_tokens") != 1
    ]
    assert asked
    for payload in asked:
        texts = [
            part.get("text", "")
            for message in payload["messages"]
            for part in (
                message["content"]
                if isinstance(message["content"], list)
                else [{"text": message["content"]}]
            )
            if isinstance(part, dict)
        ]
        assert question in texts
        assert marker not in json.dumps(payload)
    # Nothing written back: no outcome label, the source metadata unchanged.
    assert not list(e.ws.rglob("outcomes"))
    meta = json.loads((e.rollouts.demo(0) / "metadata.json").read_text())
    assert meta["eval"]["outcome"] == "success"
    # The record, the page and the agreement keep both.
    (record,) = stats.read(e.ws / "live")
    assert record["operator_label"] == {"outcome": "success", "by": "operator"}
    assert record["result"]["verdict"]["outcome"] == "failure"
    assert stats.summarize([record])["agreement"]["missed_success"]["n"] == 1
    page = api._demo_row("demo_0000", row)
    assert page["operator_label"]["outcome"] == "success"
    assert page["agreement"] == "no"


def test_the_default_pipeline_still_makes_a_temporal_run(env):
    from levi.live import generic

    e = env()
    assert e.config.pipeline.temporal is True
    e.rollouts.write(0)
    e.run()
    assert len(temporal_runs(e)) >= 1 and len(anchored_runs(e)) == 1
    assert e.state()["demos"]["demo_0000"]["temporal"]["segments"] == 5
    # The default configuration's texts and their hashes, as before.
    assert generic.manifest(live_config.Config()) == {
        "generic-guideline.v1.md": "582af9bd27deeb5d59be9c9e212a546ef819f63febbb671672419768ec039fc9",
        "generic-vocabulary.v1.json": "a47a7d1e315e1dc14f36e9f072231acc58200fd226eb6179b8d91b05fe58e548",
        "generic-definitions.v1.json": "e846f31f1c19201677e0e324ace425a5dfcd3745eab9caa9559408fe9fce7f66",
        "generic-release.v1.json": "b111653582dca15a9c8f652810af8c94ef336eb95ebb5f07d77f9437cd0abc30",
    }
