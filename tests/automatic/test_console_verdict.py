"""Terminal D's s/f/d while a forward round runs (L2-AERI-OPERATOR-EARLY-STOP):
the console reads single keys only in FORWARD_ACTIVE, drops the keys typed
before the round, ignores every other key and every key while homing,
resetting or waiting. Fake keys and fake IO with the actual orchestrator and
entry; no robot, GPU or network."""

import contextlib
import io
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aeri_harness import build, committed, config, results
from test_console import YES, Out, attested, install, journal, run
from test_fr3_bridge import no_network  # noqa: F401 - imported pytest safety fixture

from levi.automatic import console as C
from levi.automatic import console_text, metrics
from levi.automatic import fr3_bridge as bridge
from levi.automatic.orchestrator import VERDICT_NOTE


class FakeKeys:
    """A terminal whose buffer the test fills; ``log`` keeps reads and
    flushes in order, ``depth`` the cbreak nesting."""

    def __init__(self, stale=""):
        self.buffer = list(stale)
        self.log = []
        self.depth = 0
        self.entered = 0

    def type(self, text):
        self.buffer.extend(text)

    def poll(self, timeout=0.0):
        if not self.buffer:
            return None
        key = self.buffer.pop(0)
        self.log.append(("key", key))
        return key

    def flush(self):
        self.log.append(("flush", len(self.buffer)))
        self.buffer.clear()

    @contextlib.contextmanager
    def cbreak(self):
        self.depth += 1
        self.entered += 1
        try:
            yield
        finally:
            self.depth -= 1


def wired(tmp_path, *, stale="", episodes=2):
    """The actual orchestrator with a console told every committed state, as
    ``fr3_run`` wires it; the heartbeat's poll is called before each policy
    step (deterministic: the real one runs every 50 ms on its own thread)."""
    r = build(tmp_path, cfg=config(episodes=episodes))
    out = Out()
    keys = FakeKeys(stale)
    console = C.Console(keys, out)
    console.bind(r.orch, r.robot)
    r.orch.listener = console.on_state
    original = r.robot.step
    calls = {"n": 0, "before": {}}

    def step(action, *, token):
        calls["n"] += 1
        typed = calls["before"].pop(calls["n"], None)
        if typed:
            keys.type(typed)
        console.poll_verdict()
        return original(action, token=token)

    r.robot.step = step
    return SimpleNamespace(r=r, console=console, keys=keys, out=out, calls=calls)


def verdict_notes(orch):
    return [
        json.loads(e.note.detail)
        for e in orch.journal.events
        if e.record == "note" and e.note.code == VERDICT_NOTE
    ]


@pytest.mark.parametrize(
    "key, label, value",
    [
        ("s", "success", "success"),
        ("f", "failure", "failure"),
        ("d", "discard", "discarded"),
    ],
)
def test_one_key_ends_the_round_with_the_operators_verdict(tmp_path, key, label, value):
    w = wired(tmp_path)
    r, keys, out, calls = w.r, w.keys, w.out, w.calls
    calls["before"][3] = key
    assert r.orch.run() == "COMPLETED"
    first = results(r.orch)[0]
    assert first.stop_reason == "operator_verdict"
    [note] = verdict_notes(r.orch)
    assert note["label"] == label and note["source"] == "terminal"
    lines = metrics.LabelStore(r.directory).lines("operator_label")
    assert [x["value"] for x in lines] == [value]
    # The run went on: the next round ran to its own end. A discarded round
    # is replaced by one more (user decision 2026-10-16).
    rounds = 3 if key == "d" else 2
    assert len(results(r.orch)) == rounds
    assert results(r.orch)[1].stop_reason != "operator_verdict"
    text = out.getvalue()
    assert text.count("本轮进行中：按 s 记成功") == rounds  # one hint per round
    assert "已收到人工判定：" + {"s": "成功", "f": "失败", "d": "作废"}[key] in text
    # cbreak once per round, always left again
    assert keys.entered == rounds and keys.depth == 0


def test_keys_typed_before_the_round_are_dropped(tmp_path):
    w = wired(tmp_path, stale="sfd")
    r, keys = w.r, w.keys
    assert r.orch.run() == "COMPLETED"
    assert ("flush", 3) in keys.log  # dropped when the first round opened
    assert verdict_notes(r.orch) == []
    assert [x.stop_reason for x in results(r.orch)] == ["goal_verified"] * 2
    assert not (r.directory / "labels").exists()


def test_other_keys_do_nothing_and_a_later_s_still_counts(tmp_path):
    w = wired(tmp_path)
    r, keys, calls = w.r, w.keys, w.calls
    calls["before"][2] = "xq\nu"
    calls["before"][3] = "s"
    assert r.orch.run() == "COMPLETED"
    # x, q, Enter, u: read one per poll and ignored, then the s
    assert [k for kind, k in keys.log if kind == "key"][:2] == ["x", "q"]
    [note] = verdict_notes(r.orch)
    assert note["label"] == "success"
    assert results(r.orch)[0].stop_reason == "operator_verdict"


def test_keys_while_homing_do_nothing_and_are_dropped(tmp_path):
    w = wired(tmp_path)
    r, console, keys = w.r, w.console, w.keys
    original = r.robot.home
    homes = []

    def home(*, token):
        keys.type("s")
        homes.append(console._verdict_episode)
        console.poll_verdict()  # the heartbeat while the arm goes home
        return original(token=token)

    r.robot.home = home
    assert r.orch.run() == "COMPLETED"
    assert homes and all(h is None for h in homes)  # the window was closed
    assert verdict_notes(r.orch) == []
    assert [x.stop_reason for x in results(r.orch)] == ["goal_verified"] * 2
    # the s typed while homing was dropped before the next round could read it
    assert not any(kind == "key" for kind, _ in keys.log)
    # Even with the window still open, a run that is not in FORWARD_ACTIVE
    # of that same episode reads no key (the state and episode checks).
    for state, episode in (("ROBOT_HOME", "e.1"), ("FORWARD_ACTIVE", "e.2")):
        out = Out()
        stale = FakeKeys("s")
        other = C.Console(stale, out)
        other.orch = SimpleNamespace(
            state=state,
            ctx=SimpleNamespace(episode_id=episode),
            episode_verdict=lambda *a, **k: pytest.fail("no verdict here"),
        )
        other._verdict_episode = "e.1"
        other.poll_verdict()
        assert stale.buffer == ["s"], (state, episode)


def test_without_a_key_the_run_is_what_it_was(tmp_path):
    plain = build(tmp_path / "plain")
    assert plain.orch.run() == "COMPLETED"
    w = wired(tmp_path / "console")
    r, keys = w.r, w.keys
    assert r.orch.run() == "COMPLETED"
    assert committed(r.orch) == committed(plain.orch)
    assert verdict_notes(r.orch) == []
    assert keys.depth == 0


def test_an_event_pause_run_reads_no_verdict_keys():
    out = Out()
    keys = FakeKeys("s")
    console = C.Console(keys, out)
    console.orch = SimpleNamespace(
        intervention=object(),
        episode_verdict=lambda *a, **k: pytest.fail("no verdict in event_pause"),
        numbers={"forward": 1},
        config=SimpleNamespace(
            episodes=1,
            forward_max_steps=6,
            termination=SimpleNamespace(allow_early_stop=True),
        ),
        state="FORWARD_ACTIVE",
        ctx=SimpleNamespace(episode_id="e.1", steps_done=0),
    )
    console.on_state("FORWARD_ACTIVE", {"reason": "scene_ready", "episode_id": "e.1"})
    console.poll_verdict()
    assert keys.entered == 0 and keys.buffer == ["s"]
    assert "本轮进行中" not in out.getvalue()


def test_the_verdict_texts_exist_in_both_languages():
    for lang in ("zh", "en"):
        t = console_text.Texts(lang)
        for key in ("verdict_hint", "v_success", "v_failure", "v_discard"):
            assert t(key) and t(key) != key
        assert "{" not in t("verdict_taken", verdict="x")
    assert "s=成功" not in console_text.Texts("zh")("verdict_hint")


# --- the actual entry -----------------------------------------------------------------------------

HINT = "本轮进行中：按 s 记成功"


def through_the_step(monkeypatch, world):
    """Each bridge step first runs the console's verdict poll, as the
    heartbeat would between two steps (deterministic for the test)."""
    step = bridge.Robot.step

    def polled(self, action, *, token):
        console = world.console.get("it")
        if console is not None:
            console.poll_verdict()
        return step(self, action, token=token)

    monkeypatch.setattr(bridge.Robot, "step", polled)


def test_the_entry_ends_a_round_on_s_and_goes_on(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=2)
    install(
        monkeypatch,
        world,
        [("[y/n/u]", YES), (HINT, "s"), ("按 [Enter]", "\n"), ("[y/n/u]", YES)],
    )
    through_the_step(monkeypatch, world)
    assert run(world) == "COMPLETED"
    events = journal(world)
    ended = [e for e in events if e.record == "committed" and e.episode_result]
    forward = [e for e in ended if ".forward." in e.episode_id]
    assert forward[0].episode_result.stop_reason == "operator_verdict"
    assert len(forward) == 2
    assert not any(
        e.record == "committed" and e.to_state == "WAIT_HUMAN" for e in events
    )
    run_dir = Path(world.job["rollout_root"]) / ".aeri/runs/real-test"
    (line,) = metrics.LabelStore(run_dir).lines("operator_label")
    assert (line["episode_id"], line["value"]) == (forward[0].episode_id, "success")
    assert world.keys.depth == 0 and world.keys.raw_max == 1


def test_the_entry_heartbeat_polls_for_verdict_keys(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=1)
    install(monkeypatch, world, [("[y/n/u]", YES)])
    seen = []
    poll = C.Console.poll_verdict

    def recorded(self):
        seen.append(threading.current_thread().name)
        return poll(self)

    monkeypatch.setattr(C.Console, "poll_verdict", recorded)
    step = bridge.Robot.step
    slow = {"n": 0}

    def slowed(self, action, *, token):
        slow["n"] += 1
        if slow["n"] == 1:
            time.sleep(0.3)  # long enough for several heartbeat ticks
        return step(self, action, token=token)

    monkeypatch.setattr(bridge.Robot, "step", slowed)
    assert run(world) == "COMPLETED"
    assert "aeri-C2-heartbeat" in seen


def test_a_dead_terminal_cannot_make_the_poll_raise():
    class Broken(io.StringIO):
        def isatty(self):
            return True

    keys = FakeKeys()
    keys.poll = lambda timeout=0.0: (_ for _ in ()).throw(OSError("EIO"))
    console = C.Console(keys, Broken())
    console.orch = SimpleNamespace(
        state="FORWARD_ACTIVE", ctx=SimpleNamespace(episode_id="e.1")
    )
    console._verdict_episode = "e.1"
    console.poll_verdict()  # no exception reaches the heartbeat
    assert console._verdict_sent is None
