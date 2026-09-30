"""The live service end to end, without a robot or a GPU: a fake evaluation
client writes rollouts (half-written ones included), the supervisor and its
real worker process label them against a fake model server, and everything a
person or the training pool would see is checked on disk."""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest
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

    def controller(self, **probe):
        ports = probe.get("ports", lambda: set())
        vram = probe.get("vram", lambda: None)
        return controller.Controller(
            self.config,
            probes=controller.Probes(vram=vram, ports=ports),
            log=lambda *a: self.messages.append(" ".join(map(str, a))),
        )

    def run(self, **kwargs):
        ctl = self.controller()
        ctl.run(once=True, max_seconds=kwargs.pop("max_seconds", 300))
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
    assert tools.count("plans.approve") == 2 and tools.count("changes.commit") == 1
    assert {x["decision"] for x in lines} == {"allowed"} and {
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
    thread = threading.Thread(target=lambda: ctl.run(once=True, max_seconds=120))
    thread.start()
    deadline = time.time() + 60
    while time.time() < deadline:
        progress = jsonio.read(e.ws / "live/worker.json") or {}
        if progress.get("phase") in ("temporal", "anchored") and e.fake.calls:
            break
        time.sleep(0.1)
    ctl.preempt("session", "test: the supervisor is killed mid-batch")
    ctl.running = False
    ctl.wake.set()
    thread.join(30)
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
    e = env(fail=lambda n, payload: n <= 2)
    e.rollouts.write(0)
    e.run()
    state = e.state()
    # The failed requests cost an attempt or a resume, never a wrong label.
    assert state["demos"]["demo_0000"]["state"] in ("done", "mirrored", "failed")
    if state["demos"]["demo_0000"]["state"] == "done":
        assert len(e.atoms(0)) == 5


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
    thread = threading.Thread(target=lambda: ctl.run(once=True, max_seconds=120))
    thread.start()
    deadline = time.time() + 60
    while time.time() < deadline and not (ctl.worker and e.fake.calls):
        time.sleep(0.1)
    os.killpg(ctl.worker.pid, signal.SIGKILL)  # no pause, leases left behind
    thread.join(60)
    e.fake.delay = 0
    started = time.time()
    e.run()
    assert time.time() - started < 100  # not the 3 minutes a lease takes to expire
    assert {r["state"] for r in e.state()["demos"].values()} == {"done"}


def test_a_new_episode_cancels_the_request_in_flight_and_the_batch_resumes_after(env):
    """Timeshare beside a policy server: the model works between episodes, a
    new episode stops its request at once, the finished episodes stay done and
    the rest continues when the policy stops inferring."""
    e = env(delay=3.0)
    e.config.gpu.mode = "timeshare"
    e.config.vllm.adopt_external = True  # the fake server stands in for vLLM
    e.config.service.gate_poll_s = 0.3
    for n in range(2):
        e.rollouts.write(n)
    e.rollouts.session("homing")
    ctl = e.controller(ports=lambda: {8000})  # a policy server is up
    thread = threading.Thread(target=ctl.run, kwargs={"max_seconds": 240})
    thread.start()
    try:
        progress = lambda: jsonio.read(e.ws / "live/worker.json") or {}
        deadline = time.time() + 60
        while time.time() < deadline and not (
            e.fake.started and progress().get("phase") in ("temporal", "anchored")
        ):
            time.sleep(0.1)
        assert e.fake.started, "the model never got a request while the policy was idle"
        e.rollouts.session("running")  # a new episode begins: the policy infers
        deadline = time.time() + 30
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
        kinds = [
            json.loads(b)["type"]
            for (b,) in e.sql("SELECT body FROM events WHERE run_id=?", run["id"])
        ]
        assert "model_request_stopped" in kinds  # the in-flight request was cut
        sent = e.fake.started
        time.sleep(3)
        assert e.fake.started == sent  # nothing goes out while the policy infers
        assert not [c for c in e.records("changes") if c["status"] == "committed"]
        e.rollouts.session("finished")  # the evaluation ends
        deadline = time.time() + 120
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
        thread.join(60)
        ctl.shutdown()
