"""The operator console of ``fr3_run --execute`` (T-C-CONSOLE): fake keys and
fake IO only, the actual bridge and orchestrator, no robot, GPU or network."""

import contextlib
import errno
import io
import json
import re
import signal
import string
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_fr3_bridge import (  # noqa: F401 - imported pytest safety fixture
    no_network,
    rig,
)

from levi.automatic import console as C
from levi.automatic import console_text, fr3_media, metrics
from levi.automatic import fr3_run as R
from levi.automatic.adapters import human
from levi.automatic.journal import Journal


class Out(io.StringIO):
    def isatty(self):
        return True


class Keys:
    """A terminal that types ``script`` when the console has printed the
    needle of the next entry (``[(needle, "keys"), ...]``); ``stale`` is
    already in the input buffer before any prompt."""

    def __init__(self, out, script=(), stale="", log=None):
        self.out, self.script = out, list(script)
        self.buffer = list(stale)
        self.mark = 0
        self.log = log if log is not None else []
        self.flushes = 0
        self.depth = 0
        self.raw_max = 0

    def _release(self):
        if self.script:
            needle, keys = self.script[0]
            at = self.out.getvalue().find(needle, self.mark)
            if at >= 0:
                self.mark = at + len(needle)
                self.script.pop(0)
                if callable(keys):
                    keys()
                else:
                    self.buffer.extend(keys)

    def poll(self, timeout=0.0):
        self._release()
        if not self.buffer:
            return None
        key = self.buffer.pop(0)
        self.log.append(("key", key))
        return key

    def flush(self):
        self._release()  # keys typed before the flush have arrived by now
        self.flushes += 1
        self.log.append(("flush", len(self.buffer)))
        self.buffer.clear()

    @contextlib.contextmanager
    def cbreak(self):
        self.depth += 1
        self.raw_max = max(self.raw_max, self.depth)
        try:
            yield
        finally:
            self.depth -= 1


def attested(tmp_path, monkeypatch, *, episodes=2, **kwargs):
    world = rig(tmp_path, monkeypatch, "human_assisted", **kwargs)
    world.job["config"] = replace(
        world.job["config"], scene_check="operator_attested", episodes=episodes
    )
    return world


def install(monkeypatch, world, script, stale="", **options):
    out = Out()
    log = []
    keys = Keys(out, script, stale, log)
    console = {}

    def make(**kw):
        found = C.Console(keys, out, **{**kw, **options})
        console["it"] = found
        return found

    monkeypatch.setattr(C, "from_environment", lambda **kw: make(**kw))
    world.out, world.keys, world.log, world.console = out, keys, log, console
    # Frame captures are logged in order with the keys' flushes.
    imencode = world.modules[4].imencode

    def logged(*a):
        log.append(("capture", None))
        return imencode(*a)

    world.modules[4].imencode = logged
    return world


def run(world, timeout=20):
    """The actual entry on a thread: a console that waits forever fails the
    test instead of hanging it."""
    box = []
    thread = threading.Thread(
        target=lambda: box.append(
            R.execute(world.job, world.binding, load_modules=lambda b: world.modules)
        ),
        daemon=True,
    )
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), (
        "the console never returned:\n" + world.out.getvalue()[-1500:]
    )
    return box[0]


def notes(world):
    return [e.note.code for e in journal(world) if e.record == "note"]


def keys_of(world):
    return [k for kind, k in world.log if kind == "key"]


def journal(world):
    return Journal.read(Path(world.job["rollout_root"]) / ".aeri/runs/real-test").events


YES, ENTER = "y", "\n"
CONFIRM = "请按 y"
CONFIRMING = ["contract_violation_limit", "operator_stop", "policy_error"]


def full_round(extra=()):
    return [("[y/n/u]", YES), ("按 [Enter]", ENTER), ("[y/n/u]", YES), *extra]


def test_each_round_needs_one_enter_and_the_frames_follow_it(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=3)
    install(
        monkeypatch,
        world,
        [
            ("[y/n/u]", YES),
            ("按 [Enter]", ENTER),
            ("[y/n/u]", YES),
            ("按 [Enter]", ENTER),
            ("[y/n/u]", YES),
        ],
    )
    assert run(world) == "COMPLETED"
    # Three rounds: three y for the questions (the first before the run,
    # then one per reset), two Enter (none after the last round).
    assert keys_of(world) == ["y", "\n", "y", "\n", "y"]
    assert world.out.getvalue().count("按 [Enter]") == 2
    # The evidence of a post-forward check is taken after its Enter: every
    # Enter is followed by a capture before any later flush.
    order = [kind for kind, _ in world.log]
    for at, (kind, key) in enumerate(world.log):
        if (kind, key) == ("key", "\n"):
            assert order[at + 1] == "capture", world.log
    # and a flush (stale keys dropped) precedes each wait for the Enter
    assert world.keys.flushes >= 5
    out = world.out.getvalue()
    for n in (1, 2, 3):
        assert f"第 {n}/3 轮 开始" in out and f"第 {n}/3 轮 结束" in out
    assert "全部完成：3/3 轮" in out
    assert world.keys.depth == 0 and world.keys.raw_max == 1


def test_the_reset_prompt_precedes_the_capture_not_follows(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch)
    install(monkeypatch, world, full_round())
    assert run(world) == "COMPLETED"
    kinds = [k for k, _ in world.log]
    enter = world.log.index(("key", "\n"))
    assert kinds[enter + 1 : enter + 3] == ["capture", "capture"]
    # no capture between the post-forward prompt and its Enter
    prompt_flush = max(i for i in range(enter) if world.log[i][0] == "flush")
    assert "capture" not in kinds[prompt_flush:enter]


def test_n_goes_to_wait_human_and_enter_resumes_with_a_real_health_check(
    tmp_path, monkeypatch
):
    world = attested(tmp_path, monkeypatch)
    health = []
    original = R.bridge.Robot.health

    def counted(self):
        health.append(1)
        return original(self)

    monkeypatch.setattr(R.bridge.Robot, "health", counted)
    seen = {}
    resume = R.control.resume_checked

    def spy(orch, command_id, **fields):
        seen.update(fields)
        return resume(orch, command_id, **fields)

    monkeypatch.setattr(R.control, "resume_checked", spy)
    install(
        monkeypatch,
        world,
        [
            ("[y/n/u]", YES),
            ("按 [Enter]", ENTER),
            ("[y/n/u]", "n"),
            (
                "场景需复位",
                lambda: seen.update(
                    at_prompt=world.console["it"].orch.journal.next_seq
                ),
            ),
            ("按 [Enter] 续跑", ENTER),
            ("[y/n/u]", YES),
        ],
    )
    assert run(world) == "COMPLETED"
    out = world.out.getvalue()
    assert "场景需复位。请复位环境，完成后按 [Enter] 续跑" in out and "已续跑" in out
    assert seen["environment_handled"] is True and seen["health_rechecked"] is True
    # the sequence number is the one at the time the prompt was shown
    assert seen["expected_seq"] == seen["at_prompt"]
    # a real health reading was taken for that resume
    assert len(health) >= 1
    reasons = [e.reason for e in journal(world) if e.record == "committed"]
    assert "scene_reset_required" in reasons and "human_resumed" in reasons


def test_the_manual_reset_is_recorded_in_the_journal(tmp_path, monkeypatch):
    """Enter before the post_forward frames and ``y`` skip WAIT_HUMAN, so
    nothing else in the journal says a person reset the scene: the console
    writes a note (decision of 2026-10-13, "journal records the manual reset")."""
    world = attested(tmp_path, monkeypatch, episodes=3)
    install(
        monkeypatch,
        world,
        [
            ("[y/n/u]", YES),
            ("按 [Enter]", ENTER),
            ("[y/n/u]", YES),
            ("按 [Enter]", ENTER),
            ("[y/n/u]", YES),
        ],
    )
    assert run(world) == "COMPLETED"
    events = journal(world)
    found = [
        (i, e)
        for i, e in enumerate(events)
        if e.record == "note" and e.note.code == C.RESET_NOTE
    ]
    assert len(found) == 2  # one per Enter, none after the last round
    for (at, event), episode in zip(
        found, ("real-test.forward.0001", "real-test.forward.0002")
    ):
        assert episode in event.note.detail
        assert event.authority.principal_kind == "operator"
        # after the round ended, before the next round started
        before = [e for e in events[:at] if e.to_state == "ROBOT_HOME"]
        after = [e for e in events[at:] if e.to_state == "FORWARD_ACTIVE"]
        assert before and after
    assert [e.note.code for _, e in found] == [C.RESET_NOTE] * 2


def test_q_at_the_reset_prompt_records_no_manual_reset(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=3)
    install(monkeypatch, world, [("[y/n/u]", YES), ("按 [Enter]", "q")])
    run(world)
    assert C.RESET_NOTE not in notes(world)


def test_u_says_the_scene_cannot_be_judged(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch)
    install(
        monkeypatch,
        world,
        [
            ("[y/n/u]", YES),
            ("按 [Enter]", ENTER),
            ("[y/n/u]", "u"),
            ("无法判断场景", ENTER),
            ("[y/n/u]", YES),
        ],
    )
    # unknown is a WAIT_HUMAN of its own wording
    assert run(world) == "COMPLETED"
    out = world.out.getvalue()
    assert "无法判断场景（看不清/证据不足）" in out and "看不清" in out


def test_enter_alone_never_answers_a_scene_question(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=1)
    install(monkeypatch, world, [("[y/n/u]", "\n\n"), ("Enter 不作答", YES)])
    assert run(world) == "COMPLETED"
    assert world.out.getvalue().count("Enter 不作答。") >= 1
    assert "scene_check_failed" not in notes(world)


def test_a_red_health_reading_never_resumes(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch)
    original = R.bridge.Robot.health
    state = {"red": False}

    def health(self):
        if state["red"]:
            state["red"] = False  # red exactly once, at the first resume
            return {"state": "red", "reasons": ["camera dropped"]}
        return original(self)

    monkeypatch.setattr(R.bridge.Robot, "health", health)
    install(
        monkeypatch,
        world,
        [
            ("[y/n/u]", YES),
            ("按 [Enter]", ENTER),
            ("[y/n/u]", "n"),
            ("场景需复位", lambda: state.update(red=True)),
            ("按 [Enter] 续跑", ENTER),
            ("按 [Enter] 续跑", ENTER),
            ("[y/n/u]", YES),
        ],
    )
    assert run(world) == "COMPLETED"
    out = world.out.getvalue()
    assert out.count("健康检查未通过") == 1 and "camera dropped" in out
    resumed = [e for e in journal(world) if e.reason == "human_resumed"]
    # one resume, after the green reading; the red one wrote nothing
    assert len([e for e in resumed if e.record == "committed"]) == 1


def test_a_stale_enter_is_dropped_and_cannot_resume_or_stop(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch)
    holder = {}
    install(
        monkeypatch,
        world,
        [
            ("[y/n/u]", YES),
            ("按 [Enter]", ENTER),
            ("[y/n/u]", "n"),
            # old Enters pile up while the system is working: after the
            # answer was recorded and before the pause prompt.
            ("已记录", lambda: holder["keys"].buffer.extend("\n\n")),
            ("场景需复位", ENTER),
            ("[y/n/u]", YES),
        ],
    )
    holder["keys"] = world.keys
    assert run(world) == "COMPLETED"
    # the wait-human flush discarded both stale Enters
    assert ("flush", 2) in world.log
    # exactly one resume, by the Enter typed after the prompt
    resumes = [
        e
        for e in journal(world)
        if e.reason == "human_resumed" and e.record == "committed"
    ]
    assert len(resumes) == 1


def test_q_ends_the_run_at_the_reset_prompt_without_another_question(
    tmp_path, monkeypatch
):
    world = attested(tmp_path, monkeypatch, episodes=3)
    install(monkeypatch, world, [("[y/n/u]", YES), ("按 [Enter]", "q")])
    result = run(world)
    assert result != "FAULT_LOCKED"
    events = journal(world)
    assert events[-1].to_state == "COMPLETED" and events[-1].reason == "operator_stop"
    # only the opening question was asked; no post-forward frames were taken
    assert world.out.getvalue().count("场景核对：") == 1
    assert [k for k, _ in world.log].count("capture") == 2


def _page_answer(world, value=True):
    questions = Path(world.job["rollout_root"]) / ".aeri/runs/real-test/scene"
    (path,) = sorted((questions / "questions").glob("*.json"))
    human.write_answer(
        questions, json.loads(path.read_text()), {"object_at_source": value}
    )


def _forward_count(world):
    """Rounds that started (distinct episodes that reached FORWARD_ACTIVE)."""
    return len({e.episode_id for e in journal(world) if e.to_state == "FORWARD_ACTIVE"})


PAGE_IGNORED = "页面的回答不算放行"


def test_a_page_answer_beside_a_terminal_answer_is_ignored_and_noted(
    tmp_path, monkeypatch
):
    world = attested(tmp_path, monkeypatch, episodes=1)
    # the page answers false while the terminal answers y for the same question
    install(
        monkeypatch,
        world,
        [
            (
                "[y/n/u]",
                lambda: (_page_answer(world, False), world.keys.buffer.extend("y")),
            )
        ],
    )
    assert run(world) == "COMPLETED"
    # only the terminal's y counts: the run started, the page's false did
    # not send it to a person, and the page's answer is on the journal
    assert any(e.to_state == "FORWARD_ACTIVE" for e in journal(world))
    assert "场景需复位" not in world.out.getvalue()
    assert "scene_answer_page_ignored" in notes(world)


def test_a_page_answer_is_not_a_release_the_terminal_must_type_y(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=1)
    seen = []

    def typed_after_page():
        # the page has answered true; nothing has started
        seen.append(_forward_count(world))
        world.keys.buffer.extend("y")

    install(
        monkeypatch,
        world,
        [("[y/n/u]", lambda: _page_answer(world)), (PAGE_IGNORED, typed_after_page)],
    )
    assert run(world) == "COMPLETED"
    out = world.out.getvalue()
    assert seen == [0]
    assert "此题已由页面回答" not in out and "已记录，等待系统处理" in out
    assert keys_of(world) == ["y"]
    assert "scene_answer_page_ignored" in notes(world)
    assert _forward_count(world) == 1
    assert world.keys.depth == 0


def test_the_first_round_needs_a_y_at_the_terminal(tmp_path, monkeypatch):
    """The opening (initial_state) question has no Enter prompt before it;
    still only a y at the terminal starts the first round."""
    world = attested(tmp_path, monkeypatch, episodes=1)
    install(monkeypatch, world, [("[y/n/u]", ENTER + "y")])
    assert run(world) == "COMPLETED"
    out = world.out.getvalue()
    assert "按 [Enter]" not in out.split("[y/n/u]")[0]
    assert "Enter 不作答" in out
    assert keys_of(world) == ["\n", "y"]
    assert _forward_count(world) == 1


def test_after_the_reset_enter_a_page_answer_does_not_start_the_next_round(
    tmp_path, monkeypatch
):
    world = attested(tmp_path, monkeypatch, episodes=2)
    seen = []

    def page():
        _page_answer(world)

    def typed_after_page():
        seen.append(_forward_count(world))
        world.keys.buffer.extend("y")

    install(
        monkeypatch,
        world,
        [
            ("[y/n/u]", YES),
            ("按 [Enter]", ENTER),
            ("[y/n/u]", page),
            (PAGE_IGNORED, typed_after_page),
        ],
    )
    assert run(world) == "COMPLETED"
    # after the Enter the page answered: still one round only, until the y
    assert seen == [1]
    assert _forward_count(world) == 2
    assert keys_of(world) == ["y", "\n", "y"]


def test_page_answers_alone_release_no_motion(tmp_path, monkeypatch):
    """End to end on fake I/O: the page answers every scene question, the
    terminal never types a key (no Enter, no y). No round starts and no
    action reaches the arm; a stop then ends the run."""
    world = attested(tmp_path, monkeypatch, episodes=2)
    client = world.modules[0]
    queries = []
    query = client._query_policy

    def counted(*a):
        queries.append(1)
        return query(*a)

    client._query_policy = counted
    at = {}

    def stop():
        # what the page's answer released before the stop: nothing
        at["keys"] = list(keys_of(world))
        at["rounds"] = _forward_count(world)
        world.console["it"].orch.stop("test-stop-1")
        world.clock.advance(1_000_000_000)  # the collect slice ends

    def page():
        at["queries"], at["poses"] = len(queries), world.calls.count(("route", "pose"))
        _page_answer(world)
        # long enough for many collect polls; then the run is stopped
        threading.Timer(0.5, stop).start()

    # "q" only at the wait after the stop, to end the run
    install(monkeypatch, world, [("[y/n/u]", page), ("续跑", "q")])
    run(world)
    events = journal(world)
    assert at["keys"] == [] and at["rounds"] == 0
    assert not any(e.to_state == "FORWARD_ACTIVE" for e in events)
    assert len(queries) - at["queries"] == 0
    assert world.calls.count(("route", "pose")) - at["poses"] == 0
    assert "scene_answer_page_ignored" in notes(world)
    assert PAGE_IGNORED in world.out.getvalue()


@pytest.mark.parametrize("scene_check", ["operator_attested", "provider"])
def test_a_real_run_without_a_terminal_is_refused_before_any_connection(
    tmp_path, monkeypatch, scene_check
):
    """Whatever the scene check, a real run without the operator terminal is
    refused (UI-2; L2-UI2-PROVIDER-RELEASE: a provider's answer alone does
    not release motion either)."""
    world = attested(tmp_path, monkeypatch, episodes=1)
    world.job["config"] = replace(world.job["config"], scene_check=scene_check)
    monkeypatch.setattr(C, "from_environment", lambda **kw: None)
    loaded = []

    def load(binding):
        loaded.append(1)
        # past the refusal: fail at once, never reach a page-only wait
        raise AssertionError("hardware modules loaded without a terminal")

    with pytest.raises(ValueError) as found:
        R.execute(world.job, world.binding, load_modules=load)
    assert "UI-2" in str(found.value) and "页面上的回答不算放行" in str(found.value)
    assert "provider 的答案也不单独放行" in str(found.value)
    # nothing imported, no policy handshake, no RobotEnv, no run directory
    assert loaded == [] and world.calls == []
    assert not (Path(world.job["rollout_root"]) / ".aeri/runs/real-test").exists()


# --- scene_check: provider on a real run (L2-UI2-PROVIDER-RELEASE) -------------------

MODEL_SAYS = "模型判断："


def provided(tmp_path, monkeypatch, *, episodes=1, scenes=(), mode="human_assisted"):
    """A real run whose scene check is a provider: the model answers first,
    only the terminal's y/n/u releases the next round."""
    world = rig(tmp_path, monkeypatch, mode, scenes=scenes)
    world.job["config"] = replace(
        world.job["config"], scene_check="provider", episodes=episodes
    )
    return world


def _counted_queries(world):
    client = world.modules[0]
    queries = []
    query = client._query_policy

    def counted(*a):
        queries.append(1)
        return query(*a)

    client._query_policy = counted
    return queries


def test_a_provider_ready_alone_releases_no_motion(tmp_path, monkeypatch):
    """End to end on fake I/O: the model says ready, the terminal types no key.
    No round starts, no policy query, no pose reaches the arm; a stop ends it."""
    world = provided(tmp_path, monkeypatch, episodes=2)
    queries = _counted_queries(world)
    at = {}

    def stop():
        at["keys"] = list(keys_of(world))
        at["rounds"] = _forward_count(world)
        world.console["it"].orch.stop("test-stop-1")
        world.clock.advance(1_000_000_000)  # the collect slice ends

    def asked():
        at["queries"], at["poses"] = len(queries), world.calls.count(("route", "pose"))
        # the model's ready is there and shown; nothing is typed
        threading.Timer(0.5, stop).start()

    install(monkeypatch, world, [("[y/n/u]", asked), ("续跑", "q")])
    run(world)
    events = journal(world)
    assert at["keys"] == [] and at["rounds"] == 0
    assert not any(e.to_state == "FORWARD_ACTIVE" for e in events)
    assert len(queries) - at["queries"] == 0
    assert world.calls.count(("route", "pose")) - at["poses"] == 0
    out = world.out.getvalue()
    assert MODEL_SAYS + "就绪" in out and "仅供参考" in out


def test_a_provider_ready_and_a_y_start_the_round(tmp_path, monkeypatch):
    world = provided(tmp_path, monkeypatch, episodes=2)
    install(monkeypatch, world, full_round())
    assert run(world) == "COMPLETED"
    assert keys_of(world) == ["y", "\n", "y"]
    assert _forward_count(world) == 2
    # the model's answer precedes each question, the reset Enter the frames
    out = world.out.getvalue()
    assert out.count(MODEL_SAYS) == 2
    assert out.index(MODEL_SAYS) < out.index("[y/n/u]")
    enter = world.log.index(("key", "\n"))
    assert [k for k, _ in world.log][enter + 1] == "capture"
    assert "scene_model_disagreed" not in notes(world)


def test_a_provider_ready_and_an_n_do_not_start_the_round(tmp_path, monkeypatch):
    world = provided(tmp_path, monkeypatch, episodes=1)
    seen = {}
    install(
        monkeypatch,
        world,
        [
            ("[y/n/u]", "n"),
            ("场景需复位", lambda: seen.update(rounds=_forward_count(world))),
            ("续跑", "q"),
        ],
    )
    run(world)
    # the person's n is the decision: the existing n route (a person resets)
    assert seen == {"rounds": 0} and _forward_count(world) == 0
    reasons = [e.reason for e in journal(world) if e.record == "committed"]
    assert "scene_reset_required" in reasons
    found = [
        e.note.detail
        for e in journal(world)
        if e.record == "note" and e.note.code == "scene_model_disagreed"
    ]
    assert len(found) == 1
    assert "model=ready" in found[0] and "operator=reset_required" in found[0]


def test_a_provider_not_ready_and_a_y_start_the_round_and_are_noted(
    tmp_path, monkeypatch
):
    world = provided(tmp_path, monkeypatch, episodes=1, scenes=("reset_required",))
    install(monkeypatch, world, [("[y/n/u]", YES)])
    assert run(world) == "COMPLETED"
    assert _forward_count(world) == 1
    assert MODEL_SAYS + "需复位" in world.out.getvalue()
    found = [
        e.note.detail
        for e in journal(world)
        if e.record == "note" and e.note.code == "scene_model_disagreed"
    ]
    assert len(found) == 1
    assert "model=reset_required" in found[0] and "operator=ready" in found[0]


def test_a_provider_scene_of_a_reset_policy_run_needs_the_terminal_too(
    tmp_path, monkeypatch
):
    """single_reset_policy: the person's n is a reset by the reset policy (the
    existing route), and the post_reset question needs a y too."""
    world = provided(tmp_path, monkeypatch, episodes=1, mode="single_reset_policy")
    # n before the run; y after the reset; y on the scene that opens round 1
    install(monkeypatch, world, [("[y/n/u]", "n"), ("[y/n/u]", YES), ("[y/n/u]", YES)])
    assert run(world) == "COMPLETED"
    starts = [
        e.to_state
        for e in journal(world)
        if e.to_state in ("FORWARD_ACTIVE", "RESET_ACTIVE")
    ]
    assert starts[0] == "RESET_ACTIVE" and "FORWARD_ACTIVE" in starts
    assert keys_of(world) == ["n", "y", "y"]
    assert world.out.getvalue().count(MODEL_SAYS) == 3


def test_the_fault_summary_names_the_code_the_note_and_the_log(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=1, faults=("home_error",))
    install(monkeypatch, world, [("[y/n/u]", YES)])
    assert run(world) == "FAULT_LOCKED"
    out = world.out.getvalue()
    assert "运行已锁定 FAULT_LOCKED：" in out
    assert "说明：" in out and "建议：" in out
    assert str(Path(world.job["rollout_root"]) / ".aeri/runs/real-test") in out
    assert out.count("运行已锁定 FAULT_LOCKED") == 1


def test_optional_labels_use_the_same_store_and_stay_blind(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch)
    install(
        monkeypatch,
        world,
        [("[y/n/u]", YES), ("按 [Enter]", "f"), ("已标注", ENTER), ("[y/n/u]", YES)],
        labels=True,
    )
    assert run(world) == "COMPLETED"
    out = world.out.getvalue()
    run_dir = Path(world.job["rollout_root"]) / ".aeri/runs/real-test"
    (line,) = metrics.LabelStore(run_dir).lines("operator_label")
    assert (line["episode_id"], line["value"], line["by"]) == (
        "real-test.forward.0001",
        "failure",
        "operator",
    )
    assert line["reveals_verdict"] is True
    # no verdict word before the label; the reveal comes after it
    before = out[: out.index("已标注")]
    assert "自动判定（标注后可见）" not in before
    assert "自动判定（标注后可见）" in out
    assert "标注后显示自动判定" in before


def test_labels_are_off_by_default(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch)
    install(
        monkeypatch,
        world,
        [("[y/n/u]", YES), ("按 [Enter]", "s" + ENTER), ("[y/n/u]", YES)],
    )
    assert run(world) == "COMPLETED"
    run_dir = Path(world.job["rollout_root"]) / ".aeri/runs/real-test"
    assert not (run_dir / "labels").exists()
    assert "s=成功" not in world.out.getvalue()


# --- units -------------------------------------------------------------------------------------


class NotATerminal(io.StringIO):
    def isatty(self):
        return False


def test_no_terminal_means_no_console():
    assert C.from_environment(stdin=NotATerminal(), stdout=Out(), environ={}) is None
    assert C.from_environment(stdin=Out(), stdout=NotATerminal(), environ={}) is None
    assert (
        C.from_environment(stdin=Out(), stdout=Out(), environ={C.ENV_SWITCH: "0"})
        is None
    )
    assert isinstance(
        C.from_environment(stdin=Out(), stdout=Out(), environ={}, keys=object()),
        C.Console,
    )


def question(names=("a", "b")):
    return {
        "request_id": "r1",
        "nonce": "n",
        "frames_sha256": "f",
        "target": "post_forward",
        "predicates": [{"name": n, "text": n.upper(), "required": True} for n in names]
        + [{"name": "opt", "text": "OPT", "required": False}],
        "frames": [{"view": "side", "file": "evidence/frames/x.jpg"}],
    }


def test_terminal_transport_reads_one_key_per_predicate_and_never_blocks(tmp_path):
    out = Out()
    keys = Keys(out)
    transport = human.TerminalTransport(
        keys, lambda t: out.write(t + "\n"), run_dir=tmp_path
    )
    assert transport.take_answers() == []  # nothing asked: nothing read
    transport.ask(question())
    text = out.getvalue()
    assert str(tmp_path / "evidence/frames/x.jpg") in text and "[1/2] a（A）" in text
    assert "opt" not in text.split("按键")[0]  # only required predicates are asked
    keys.buffer.extend("\ny")  # Enter is ignored, then a=true
    assert transport.take_answers() == []
    keys.buffer.extend("bn")  # back, then a=false
    assert transport.take_answers() == []
    keys.buffer.extend("U")  # b=cannot tell (capital letters count)
    (answer,) = transport.take_answers()
    assert answer == human.answer_for(question(), {"a": False, "b": None})
    assert keys.depth == 0 and transport.take_answers() == []


def test_terminal_transport_stops_reading_when_stopped():
    out = Out()
    keys = Keys(out)
    stop = []
    transport = human.TerminalTransport(
        keys, lambda t: None, stopped=lambda: bool(stop)
    )
    transport.ask(question(("a",)))
    stop.append(1)
    keys.buffer.extend("y")
    assert transport.take_answers() == []
    assert keys.buffer == ["y"]
    transport.withdraw("r1", "stopped")
    assert keys.depth == 0


def test_terminal_transport_warns_once_per_status_change():
    said = []
    status = ["red"]
    transport = human.TerminalTransport(
        Keys(Out()), said.append, status=lambda: status[0]
    )
    transport.ask(question(("a",)))
    for _ in range(3):
        transport.take_answers()
    status[0] = None
    transport.take_answers()
    assert said.count("red") == 1


def test_summary_screen_lists_what_the_operator_is_authorising(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=4)
    text = C.summary_text(world.job)
    for needle in (
        "real-test",
        "不含复位策略",
        "4 轮",
        "最多 6 步",
        "stack",
        "forward",
        "/models/forward",
        "起始位姿检查",
        "q",
    ):
        assert needle in text


def test_stdin_keys_on_a_real_pty():
    import os
    import pty
    import termios

    master, slave = pty.openpty()
    try:
        keys = C.StdinKeys(slave)
        assert keys.poll(0) is None
        with keys.cbreak():
            os.write(master, b"\x1b[D")
            assert keys.poll(0.5) == "\x1b"  # an arrow key is one ignorable key
            assert keys.poll(0.05) is None
            os.write(master, b"q")
            assert keys.poll(0.5) == "q"
        before = termios.tcgetattr(slave)
        with keys.cbreak():
            assert termios.tcgetattr(slave)[3] & termios.ICANON == 0
        assert termios.tcgetattr(slave) == before
        os.write(master, b"stale\n")
        time.sleep(0.05)
        keys.flush()
        assert keys.poll(0.05) is None
    finally:
        os.close(master)
        os.close(slave)


# --- Ctrl+C ----------------------------------------------------------------------------------------
# The run is on the main thread, as in ``fr3_run``: ``signal.raise_signal`` runs
# the entry's real SIGINT handler (a flag; the heartbeat then holds the arm and
# stops the run), and a hung console fails the test through SIGALRM.


def ctrl_c():
    signal.raise_signal(signal.SIGINT)


def tick_with_the_clock(monkeypatch, world):
    """The fake clock only moves when told to: a scene provider that waits on
    it would never end a slice, so its waits advance it (a little real time
    each, so the heartbeat thread can deliver the stop)."""
    make = C.human_scene

    def with_waits(*args, **kwargs):
        scene = make(*args, **kwargs)
        scene.wait = lambda ns: (time.sleep(0.005), world.clock.advance(ns))
        return scene

    monkeypatch.setattr(C, "human_scene", with_waits)


def run_main(world, timeout=20):
    def hung(signum, frame):
        raise AssertionError(
            "the console never returned:\n" + world.out.getvalue()[-1500:]
        )

    before = signal.signal(signal.SIGALRM, hung)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        return R.execute(world.job, world.binding, load_modules=lambda b: world.modules)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, before)


def moves(world):
    return [c for c in world.calls if c == ("route", "pose")]


def assert_stopped_safely(world, state, *, may_lock=False):
    """No unhandled exception (we got here), the arm held, the run did not
    fault (``may_lock``: it may lock with ``safety_stop``, which is what a
    motion refused under the stop flag does today, and the screen must say it
    was the Ctrl+C), the terminal and the handlers were restored, and the
    journal says who stopped it and where."""
    if may_lock and state == "FAULT_LOCKED":
        assert "此锁定由 Ctrl+C 触发" in world.out.getvalue()
    else:
        assert state != "FAULT_LOCKED", world.out.getvalue()[-1500:]
    assert world.job["interrupted_by"] == signal.SIGINT
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    assert world.keys.depth == 0
    assert world.calls[-1][0] == "closed"  # the robot environment was released
    stopped_at = len(moves(world))
    events = journal(world)
    last = [e for e in events if e.record == "committed"][-1]
    assert last.to_state in ("COMPLETED", "WAIT_HUMAN") or (
        may_lock and (last.to_state, last.reason) == ("FAULT_LOCKED", "safety_stop")
    )
    stop = [e for e in events if e.record == "note" and e.note.code == C.INTERRUPT_NOTE]
    assert len(stop) == 1 and "SIGINT" in stop[0].note.detail
    assert "Ctrl+C" in world.out.getvalue()
    return stopped_at, stop[0].note.detail


def test_ctrl_c_at_a_scene_question_stops_without_moving(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=2)
    install(monkeypatch, world, [("[y/n/u]", ctrl_c)])
    tick_with_the_clock(monkeypatch, world)
    state = run_main(world)
    _, where = assert_stopped_safely(world, state)
    assert "scene question" in where
    assert moves(world) == []  # the opening question was never answered
    assert not any(e.to_state == "FORWARD_ACTIVE" for e in journal(world))


def test_ctrl_c_while_waiting_for_enter_stops_the_run(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=3)
    install(monkeypatch, world, [("[y/n/u]", YES), ("按 [Enter]", ctrl_c)])
    tick_with_the_clock(monkeypatch, world)
    state = run_main(world)
    _, where = assert_stopped_safely(world, state)
    assert "reset prompt" in where
    # the first round ran, the second never started, no second question
    assert world.out.getvalue().count("场景核对：") == 1
    committed = [e for e in journal(world) if e.record == "committed"]
    assert [e.to_state for e in committed].count("FORWARD_ACTIVE") == 1
    assert C.RESET_NOTE not in notes(world)


def test_ctrl_c_in_wait_human_stops_the_run(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=2)
    install(
        monkeypatch,
        world,
        [
            ("[y/n/u]", YES),
            ("按 [Enter]", ENTER),
            ("[y/n/u]", "n"),
            ("场景需复位", ctrl_c),
        ],
    )
    tick_with_the_clock(monkeypatch, world)
    state = run_main(world)
    _, where = assert_stopped_safely(world, state)
    assert "waiting for a person" in where
    assert [e for e in journal(world) if e.record == "committed"][
        -1
    ].reason == "operator_stop"


def test_ctrl_c_while_the_policy_runs_holds_the_arm(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=2)
    install(monkeypatch, world, [("[y/n/u]", YES)])
    tick_with_the_clock(monkeypatch, world)
    client = world.modules[0]
    query, seen = client._query_policy, []

    def interrupted(*args):
        # The policy warm-up of the entry queries once before the entry's
        # signal handlers exist: Ctrl+C belongs to the first query after.
        if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
            return query(*args)
        seen.append(1)
        if len(seen) == 1:
            ctrl_c()
        return query(*args)

    client._query_policy = interrupted
    state = run_main(world)
    assert seen, "the entry never queried the policy under its handlers"
    sent, where = assert_stopped_safely(world, state, may_lock=True)
    assert "while running" in where
    # the stop flag is read before every motion: the interrupted chunk and
    # whatever followed did not reach the robot (only the clearerr/pose pairs
    # of the guarded start, if any, are allowed)
    assert sent == 0, world.calls
    assert not any(
        e.to_state == "FORWARD_ACTIVE" and e.episode_id.endswith("0002")
        for e in journal(world)
    )


def test_ctrl_c_at_the_execute_phrase_prompt_is_a_clean_cancel(
    tmp_path, monkeypatch, capsys
):
    """Before any connection: no traceback, a distinct exit code, and the
    robot code is never reached."""
    import builtins

    called = []
    monkeypatch.setattr(R, "execute", lambda *a, **k: called.append(1))
    world = attested(tmp_path, monkeypatch)
    monkeypatch.setattr(R.sys, "stdin", Out())
    monkeypatch.setattr(R, "load_binding", lambda path, job: (world.binding, "d" * 64))
    monkeypatch.setattr(R.cli, "load_job", lambda path: world.job)
    monkeypatch.setattr(
        builtins, "input", lambda prompt="": (_ for _ in ()).throw(KeyboardInterrupt)
    )
    code = R.main(
        [
            "--config",
            "job.json",
            "--binding",
            "b.json",
            "--execute",
            "--expect-plan",
            world.job["plan_sha256"],
            "--expect-binding",
            "d" * 64,
        ]
    )
    assert code == 130 and called == []
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "已在连接机器人之前取消" in err and "Cancelled" not in err


# --- words, the state table and the prompt line (T-C-CONSOLE, step 4 and 5) --------------------------


def fields(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_the_catalogues_have_the_same_keys_and_placeholders():
    zh, en = console_text.TEXT["zh"], console_text.TEXT["en"]
    assert set(zh) == set(en)
    for key in zh:
        assert fields(zh[key]) == fields(en[key]), key
    assert console_text.language({}) == "zh"
    assert console_text.language({console_text.ENV_LANG: "EN"}) == "en"
    assert console_text.language({console_text.ENV_LANG: "fr"}) == "zh"


def test_every_sentence_the_code_asks_for_exists():
    """Keys are built from journal codes: each code the tables and the
    metrics name has its sentence, so a new code fails here, not on the
    operator's screen as a raw key."""
    keys = {row.say for row in C.STATUS}
    for happened, todo in [*C.WAIT_REASONS.values(), C.WAIT_OTHER]:
        keys |= {happened, todo}
    keys |= {"ended_" + x for x in metrics.ENDED_BY}
    keys |= {"verdict_" + x for x in ("success", "failure", "undecided", "none")}
    keys |= {"fault_" + x for x in C.FAULT_CODES} | {
        "advice_" + x for x in C.FAULT_CODES
    }
    keys |= {"target_" + x for x in ("initial_state", "post_forward", "post_reset")}
    keys |= {"scene_withdrawn_" + x for x in ("answered", "timeout", "stopped")}
    keys |= {"scene_model_" + x for x in (*C.MODEL_DECISIONS, "unavailable")}
    # subtask.start_home confirmations, built as "startup_home_" + step (console.py)
    keys |= {
        "startup_home_" + x
        for x in ("object_handled", "path_clear", "gripper_will_open")
    }
    # and every literal key in the two modules
    source = (
        Path(C.__file__).read_text()
        + Path(human.__file__).read_text()
        + Path(R.__file__).read_text()
    )
    keys |= set(re.findall(r'\bt\(\s*"([a-z_]+)"', source))
    keys |= set(re.findall(r'self\.t\(\s*"([a-z_]+)"', source))
    keys |= set(re.findall(r'operator_text\(\s*"([a-z_]+)"', source))
    keys -= {
        "scene_withdrawn_",
        "scene_model_",
        "ended_",
        "verdict_",
        "fault_",
        "advice_",
        "target_",
        "startup_home_",
    }
    for lang, catalogue in console_text.TEXT.items():
        assert not [k for k in keys if k not in catalogue], (
            lang,
            sorted(keys - set(catalogue)),
        )


def test_a_home_that_sent_nothing_has_its_own_wait_in_both_languages():
    """L2-HOME-EXECNO-ORCH: WAIT_HUMAN ``home_not_executed`` says the arm did
    not move and asks for a check; one Enter is not enough to resume."""
    happened, todo = C.WAIT_REASONS["home_not_executed"]
    assert (happened, todo) != C.WAIT_OTHER
    assert "home_not_executed" in C.CONFIRM_REASONS
    zh, en = console_text.TEXT["zh"], console_text.TEXT["en"]
    # SHOULD-3: the arm is not home but where the last episode ended, and
    # VERIFY_INITIAL does not check the start pose again: say so.
    assert "首条命令" in zh[happened] and "没有回 Home" in zh[happened]
    assert "上一片段结束的位置" in zh[happened]
    assert "setup.md" in zh[todo] and "y" in zh[todo]
    assert "first command" in en[happened] and "did not go home" in en[happened]
    assert "where the last episode ended" in en[happened]
    assert "setup.md" in en[todo] and "y" in en[todo]
    # SHOULD-4: the resume checks the start pose by itself.
    assert "自动检查" in zh[todo] and "checks automatically" in en[todo]


def test_an_arm_off_home_after_the_wait_has_its_own_wait_in_both_languages():
    happened, todo = C.WAIT_REASONS["home_pose_not_reached"]
    assert "home_pose_not_reached" in C.CONFIRM_REASONS
    assert "home_pose_not_reached" not in C.ROUTINE_WAITS  # its note is shown
    assert "start_pose_checked" in C.DISPLAY_NOTES
    zh, en = console_text.TEXT["zh"], console_text.TEXT["en"]
    assert "不在 Home 位姿" in zh[happened] and "setup.md" in zh[todo]
    assert "not at the home pose" in en[happened] and "setup.md" in en[todo]
    from levi.automatic import state_machine as sm

    assert "home_pose_not_reached" in sm.TO_HUMAN
    from levi.automatic import state_machine as sm

    assert any(
        "home_not_executed" in rule.reasons
        for rule in sm.TRANSITIONS.get(("ROBOT_HOME", "WAIT_HUMAN"), [])
    )


def event(record, to_state=None, reason=None, note=None):
    return SimpleNamespace(
        record=record,
        to_state=to_state,
        reason=reason,
        note=None if note is None else SimpleNamespace(code=note),
    )


def test_the_state_table_reads_synthetic_journals_including_the_home_gate_ones():
    """The three reasons of ``fix/aeri-home-gate`` are in the table before
    that branch is merged: committed ``robot_home_reached``, note
    ``robot_homed``, a run that completes right after a single segment's
    success, and WAIT_HUMAN ``scene_reset_required`` of a multi-segment run."""

    def say(*events):
        row = C.status_row(*C.latest(list(events)))
        return None if row is None else row.say

    commit = lambda state, reason: event("committed", state, reason)
    assert say(commit("ROBOT_HOME", "goal_verified")) == "st_home"
    assert say(commit("SCENE_ASSESS", "robot_home_reached")) == "st_home_done"
    assert say(commit("ROBOT_HOME", "robot_home_reached")) == "st_home_done"
    # the note beside the commit of the home (before or after it)
    assert (
        say(
            commit("FORWARD_FINALIZE", "goal_verified"),
            event("note", note="robot_homed"),
            commit("ROBOT_HOME", "goal_verified"),
        )
        == "st_home_done"
    )
    assert (
        say(commit("ROBOT_HOME", "x"), event("note", note="robot_homed"))
        == "st_home_done"
    )
    assert say(commit("COMPLETED", "run_completed")) == "st_completed"
    assert say(commit("COMPLETED", "operator_stop")) == "st_stopped"
    assert say(commit("FORWARD_JUDGING", "inference_paused")) == "st_event_pause"
    assert say(commit("FORWARD_ACTIVE", "scene_ready")) == "st_forward"
    assert say(commit("WAIT_HUMAN", "scene_reset_required")) is None
    assert C.WAIT_REASONS["scene_reset_required"] == (
        "wait_scene_reset_required",
        "todo_reset_resume",
    )
    # nothing committed yet, an unknown reason: no row, no crash
    assert C.latest([]) == (None, None, None)
    assert say(commit("PREFLIGHT_X", "?")) is None


def stub(episodes=3, allow_early_stop=True, number=1):
    out = Out()
    console = C.Console(Keys(out), out)
    console.orch = SimpleNamespace(
        numbers={"forward": number},
        config=SimpleNamespace(
            episodes=episodes,
            forward_max_steps=6,
            termination=SimpleNamespace(allow_early_stop=allow_early_stop),
        ),
        ctx=SimpleNamespace(steps_done=4),
        completed_forward=number,
    )
    return console, out


def test_banners_say_what_happened_without_reasons_or_verdicts_before_the_label():
    console, out = stub()
    console.on_state("FORWARD_ACTIVE", {"reason": "scene_ready", "episode_id": "e.1"})
    console.on_state("FORWARD_JUDGING", {"reason": "inference_paused"})
    console.on_state("FORWARD_ACTIVE", {"reason": "inference_resumed"})  # silent
    console.on_state("ROBOT_HOME", {"reason": "goal_verified", "episode_id": "e.1"})
    text = out.getvalue()
    assert "── 第 1/3 轮 开始  (e.1) ──" in text
    assert "[第 1/3 轮] 事件触发，策略已暂停，视觉模型判定中" in text
    assert text.count("开始") == 1  # the resume is not a new round
    assert "标注后显示" in text or "不显示自动判定" in text
    for hidden in (
        "goal_verified",
        "ROBOT_HOME",
        "FINALIZE",
        "步数",
        "提前结束",
        "成功",
    ):
        assert hidden not in text, hidden


def test_without_an_early_stop_the_banner_keeps_steps_and_how_it_ended():
    console, out = stub(allow_early_stop=False)
    console.on_state("ROBOT_HOME", {"reason": "horizon_exhausted", "episode_id": "e.1"})
    assert "步数 4/6；结束方式 步数上限" in out.getvalue()


def test_a_run_that_completes_or_is_stopped_says_so():
    console, out = stub(number=3)
    console.on_state("COMPLETED", {"reason": "run_completed"})
    console.on_state("COMPLETED", {"reason": "operator_stop", "stopped": True})
    text = out.getvalue()
    assert "[第 3/3 轮] 全部完成：3/3 轮" in text
    assert "[第 3/3 轮] 已按停止请求结束：完成 3/3 轮" in text


def test_the_prompt_line_has_the_round_what_happened_and_what_to_do(
    tmp_path, monkeypatch
):
    world = attested(tmp_path, monkeypatch, episodes=2)
    install(monkeypatch, world, full_round())
    assert run(world) == "COMPLETED"
    out = world.out.getvalue()
    assert "[第 1/2 轮] 机械臂已回位。请复位环境，完成后按 [Enter]（q 结束）" in out
    assert "[第 1/2 轮] 场景核对：forward 片段结束后" in out
    assert "[第 1/2 轮] 场景核对：运行开始前" in out  # the first question: round 1
    assert out.count("按 [Enter]") == 1  # one Enter in a two-round run
    # a person never sees an internal state name or reason in the flow
    for internal in ("ROBOT_HOME", "FORWARD_FINALIZE", "goal_verified", "SCENE_ASSESS"):
        assert internal not in out


def test_the_question_that_opens_a_round_carries_that_rounds_number():
    console, _ = stub(number=1)
    assert console.tag("post_forward") == "[第 1/3 轮]"
    assert console.tag("initial_state") == "[第 2/3 轮]"
    console, _ = stub(number=3)  # never past the last round
    assert console.tag("initial_state") == "[第 3/3 轮]"
    console, _ = stub(number=0)
    assert console.tag("initial_state") == "[第 1/3 轮]"
    assert console.tag() == "[准备]"


def test_the_english_terminal_has_no_chinese_left(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=2)
    install(
        monkeypatch,
        world,
        [
            ("[y/n/u]", YES),
            ("press [Enter]", ENTER),
            ("[y/n/u]", "n"),
            ("needs a reset", ENTER),
            ("[y/n/u]", YES),
        ],
        lang="en",
    )
    assert run(world) == "COMPLETED"
    out = world.out.getvalue()
    assert (
        "[Round 1/2] The arm is home. Reset the environment, then press [Enter] (q ends)"
        in out
    )
    assert (
        "The scene needs a reset. Reset the environment, then press [Enter] to resume (q ends)"
        in out
    )
    assert "Resumed" in out and "All done: 2/2 rounds" in out
    assert not re.search(r"[\u4e00-\u9fff]", out), out


def test_the_english_fault_summary_and_start_screen(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=1, faults=("home_error",))
    install(monkeypatch, world, [("[y/n/u]", YES)], lang="en")
    assert run(world) == "FAULT_LOCKED"
    out = world.out.getvalue()
    assert "Run locked FAULT_LOCKED:" in out and "Advice:" in out
    assert not re.search(r"[\u4e00-\u9fff]", out), out
    text = C.summary_text(world.job, lang="en")
    assert "no reset policy" in text and "Stop any time: Ctrl+C" in text
    assert not re.search(r"[\u4e00-\u9fff]", text), text


def test_the_language_comes_from_the_environment():
    en = C.from_environment(
        stdin=Out(), stdout=Out(), environ={C.ENV_LANG: "en"}, keys=object()
    )
    zh = C.from_environment(stdin=Out(), stdout=Out(), environ={}, keys=object())
    assert (en.t.lang, zh.t.lang) == ("en", "zh")


def _main_stopped_by_signal(tmp_path, monkeypatch, world=None, execute=None):
    import builtins

    world = world or attested(tmp_path, monkeypatch)
    phrase = "execute " + world.job["config"].run_id

    def stopped_by_ctrl_c(job, binding):
        job["interrupted_by"] = int(signal.SIGINT)  # what the entry leaves behind
        return "WAIT_HUMAN"

    monkeypatch.setattr(R, "execute", execute or stopped_by_ctrl_c)
    monkeypatch.setattr(R.sys, "stdin", Out())
    monkeypatch.setattr(R, "load_binding", lambda path, job: (world.binding, "d" * 64))
    monkeypatch.setattr(R.cli, "load_job", lambda path: dict(world.job))
    monkeypatch.setattr(R.launch, "job_fact", lambda path: {"sha256": "x"})
    monkeypatch.setattr(R.launch, "index_path", lambda run_id: tmp_path / "none")
    monkeypatch.setattr(builtins, "input", lambda prompt="": phrase)
    code = R.main(
        [
            "--config",
            "job.json",
            "--binding",
            "b.json",
            "--execute",
            "--expect-plan",
            world.job["plan_sha256"],
            "--expect-binding",
            "d" * 64,
        ]
    )
    return code


def test_a_run_stopped_by_a_signal_exits_with_128_plus_the_signal(
    tmp_path, monkeypatch, capsys
):
    code = _main_stopped_by_signal(tmp_path, monkeypatch)
    assert code == 128 + int(signal.SIGINT)
    err = capsys.readouterr().err
    assert "已被信号 2 停止" in err and "Stopped by signal" not in err


def test_the_exit_line_is_english_with_the_english_console(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv(console_text.ENV_LANG, "en")
    assert _main_stopped_by_signal(tmp_path, monkeypatch) == 128 + int(signal.SIGINT)
    err = capsys.readouterr().err
    assert "Stopped by signal 2; final state WAIT_HUMAN." in err
    assert not re.search(r"[\u4e00-\u9fff]", err), err


# --- Enter never moves a robot that preflight refuses --------------------------------------------


@pytest.mark.parametrize("latched", [False, True])
@pytest.mark.parametrize(
    "reason,sentence",
    [
        ("policy_error", "策略不可用或出错"),
        ("preflight_failed", "预检失败"),
        ("contract_violation_limit", "判定方多次违反契约"),
    ],
)
def test_enter_in_wait_human_cannot_move_a_robot_that_preflight_refuses(
    tmp_path, monkeypatch, reason, sentence, latched
):
    """Enter resumes into PREFLIGHT (``resume_checked``); a refused preflight
    ends in WAIT_HUMAN ``preflight_failed`` again, a latched robot in
    FAULT_LOCKED ``safety_stop``. Either way no motion command is sent and no
    episode starts, and the journal holds the resume and the refusal."""
    world = attested(tmp_path, monkeypatch, episodes=2)
    O = R.Orchestrator
    original, seen = O._preflight, []

    def preflight(self):
        if not seen:
            seen.append(1)
            self._to_human(reason)  # the run waits for a person, for this reason
        else:
            original(self)

    monkeypatch.setattr(O, "_preflight", preflight)
    monkeypatch.setattr(
        R.bridge.Robot,
        "preflight",
        lambda self: {"ok": False, "reasons": ["refused by the test"]},
    )
    if latched:
        monkeypatch.setattr(
            R.bridge.Robot, "latch_state", lambda self: "latched" if seen else None
        )
    script = [("按 [Enter] 续跑", ENTER)]
    if reason in CONFIRMING:
        script.append((CONFIRM, "y"))  # F4: a second, explicit key
    script += [] if latched else [("按 [Enter] 续跑", "q")]
    install(monkeypatch, world, script)
    state = run(world)
    out = world.out.getvalue()
    assert sentence in out
    events = journal(world)
    committed = [e for e in events if e.record == "committed"]
    assert moves(world) == []
    assert not any(e.to_state == "FORWARD_ACTIVE" for e in committed)
    resumed = [e for e in committed if e.reason == "human_resumed"]
    assert len(resumed) == 1 and resumed[0].to_state == "PREFLIGHT"
    after = committed[committed.index(resumed[0]) + 1 :]
    if latched:
        assert state == "FAULT_LOCKED"
        assert (after[-1].to_state, after[-1].reason) == ("FAULT_LOCKED", "safety_stop")
        assert "运行已锁定 FAULT_LOCKED：safety_stop" in out
    else:
        assert (
            after[0].to_state == "WAIT_HUMAN" and after[0].reason == "preflight_failed"
        )
        assert state != "FAULT_LOCKED"


# --- review fixes (L2-CONSOLE-REVIEW-FIXES) ------------------------------------------------------


def test_a_console_close_failure_still_releases_everything_else(tmp_path, monkeypatch):
    """F1: ``Console.close`` raising (a dead terminal: OSError) must not skip
    the heartbeat join, the fence, the orchestrator, the recorder or the
    restoring of the SIGINT/SIGTERM handlers."""
    world = attested(tmp_path, monkeypatch, episodes=1)
    install(monkeypatch, world, [("[y/n/u]", YES)])
    closed = []

    def dead_terminal(self, state=None):
        raise OSError("terminal gone")

    monkeypatch.setattr(C.Console, "close", dead_terminal)
    for owner, name in (
        (R.sm.MotionFence, "revoke"),
        (R.Orchestrator, "close"),
        (fr3_media.Recorder, "close"),
    ):
        original = getattr(owner, name)

        def spy(self, *a, _o=original, _n=owner.__name__ + "." + name, **kw):
            closed.append(_n)
            return _o(self, *a, **kw)

        monkeypatch.setattr(owner, name, spy)
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    with contextlib.suppress(OSError):
        run_main(world)
    assert {s: signal.getsignal(s) for s in before} == before
    assert set(closed) == {
        "MotionFence.revoke",
        "Orchestrator.close",
        "Recorder.close",
    }


def test_a_dead_terminal_cannot_make_finish_or_close_raise():
    """F1: the fault summary and ``close`` write to a terminal that may be
    gone; that must never raise into the entry's cleanup."""

    class Dead(Out):
        def write(self, text):
            raise OSError("terminal gone")

    out = Dead()
    console = C.Console(Keys(out), out)
    console.orch = SimpleNamespace(
        state="FAULT_LOCKED",
        halted="x",
        corrupt=None,
        notes=[("n", "d")],
        journal=SimpleNamespace(events=[], corrupt=None, directory="/tmp/x"),
    )
    console.close()


def test_progress_hides_the_step_count_while_an_early_stop_is_allowed():
    """F2: with an early stop the step count of a running round would show
    that it ended early; before the label of that episode exists the line
    only says it is running (once per round)."""
    console, out = stub()
    console.orch.state = "FORWARD_ACTIVE"
    console.orch.ctx = SimpleNamespace(episode_id="e.1", steps_done=1)
    console.tick()
    for steps in (2, 3, 5):
        console.orch.ctx.steps_done = steps
        console.tick()
    text = out.getvalue()
    assert "运行中" in text and text.count("运行中") == 1
    assert not re.search(r"步 ?\d|\d+/6", text.replace("1/3", "")), text
    console.orch.ctx = SimpleNamespace(episode_id="e.2", steps_done=1)
    console.tick()
    assert out.getvalue().count("运行中") == 2


def test_progress_keeps_the_step_count_without_an_early_stop():
    console, out = stub(allow_early_stop=False)
    console.orch.state = "FORWARD_ACTIVE"
    console.orch.ctx = SimpleNamespace(episode_id="e.1", steps_done=2)
    console.tick()
    console.orch.ctx.steps_done = 3
    console.tick()
    assert "步 2/6" in out.getvalue() and "步 3/6" in out.getvalue()


def _noted_console(notes_, *, allow_early_stop=True):
    console, out = stub(allow_early_stop=allow_early_stop)
    commit = SimpleNamespace(
        record="committed", to_state="FAULT_LOCKED", reason="watchdog_timeout"
    )
    console.orch.state = "FAULT_LOCKED"
    console.orch.halted = None
    console.orch.notes = list(notes_)
    console.orch.journal = SimpleNamespace(
        events=[commit], corrupt=None, directory="/tmp/run"
    )
    return console, out


VERDICT_NOTES = [
    ("early_stop_disputed", "final judgement: success VERDICT-LEAK"),
    ("final_judgement_skipped", "VERDICT-LEAK skipped"),
    ("judgement_timeout", "VERDICT-LEAK timeout"),
    ("contract_violation", "VERDICT-LEAK violation"),
]


def test_recent_notes_show_only_whitelisted_codes_and_no_verdicts():
    """F3: the notes under a WAIT_HUMAN line come from a whitelist; a
    judgement note (it carries the final verdict) is not on it."""
    console, _ = _noted_console(
        [("recorder_error", "disk full"), *VERDICT_NOTES, ("scene_answer_x", "y")]
    )
    shown = console._recent_notes(count=10)
    assert shown == [("recorder_error", "disk full")]


def test_the_fault_summary_leaves_out_judgement_notes_before_the_label():
    console, out = _noted_console([("hold_failed", "arm busy"), *VERDICT_NOTES])
    console.finish()
    text = out.getvalue()
    assert "hold_failed: arm busy" in text
    assert "VERDICT-LEAK" not in text and "early_stop_disputed" not in text


def test_judgement_notes_stay_off_the_terminal_without_an_early_stop_too():
    console, out = _noted_console(VERDICT_NOTES, allow_early_stop=False)
    console.finish()
    assert "early_stop_disputed" not in out.getvalue()  # still not whitelisted


# --- F4: the dangerous waits need a second key ------------------------------------------------------


def _wait_for(world, reason):
    """The first PREFLIGHT sends the run to WAIT_HUMAN for ``reason``."""
    O = R.Orchestrator
    original, seen = O._preflight, []

    def preflight(self):
        if not seen:
            seen.append(1)
            self._to_human(reason)
        else:
            original(self)

    return O, preflight


def _resumes(world):
    return [
        e
        for e in journal(world)
        if e.record == "committed" and e.reason == "human_resumed"
    ]


@pytest.mark.parametrize("reason", CONFIRMING)
def test_two_quick_enters_do_not_resume_a_dangerous_wait(tmp_path, monkeypatch, reason):
    """F4: the second Enter of a double press is dropped by the flush that
    precedes the confirmation prompt, and Enter is never the confirmation."""
    world = attested(tmp_path, monkeypatch, episodes=2)
    O, preflight = _wait_for(world, reason)
    monkeypatch.setattr(O, "_preflight", preflight)
    install(monkeypatch, world, [("按 [Enter]", "\n\n"), (CONFIRM, "q")])
    run(world, timeout=6)
    out = world.out.getvalue()
    assert out.count(CONFIRM) == 1
    assert _resumes(world) == []
    assert world.keys.flushes >= 2


@pytest.mark.parametrize("reason", CONFIRMING)
def test_a_y_typed_before_the_confirmation_prompt_is_discarded(
    tmp_path, monkeypatch, reason
):
    """F4: Enter and y pressed together (y already in the buffer when the
    confirmation prompt appears) must not confirm."""
    world = attested(tmp_path, monkeypatch, episodes=2)
    O, preflight = _wait_for(world, reason)
    monkeypatch.setattr(O, "_preflight", preflight)
    install(monkeypatch, world, [("按 [Enter]", "\ny"), (CONFIRM, "q")])
    run(world, timeout=6)
    assert _resumes(world) == []
    assert ("flush", 1) in world.log  # the pre-seeded y was in the buffer


@pytest.mark.parametrize("reason", CONFIRMING)
def test_enter_then_y_resumes_through_preflight(tmp_path, monkeypatch, reason):
    """F4: the confirmed resume is the normal one: ``resume_checked`` into
    PREFLIGHT, which the refusing robot still turns down."""
    world = attested(tmp_path, monkeypatch, episodes=2)
    O, preflight = _wait_for(world, reason)
    monkeypatch.setattr(O, "_preflight", preflight)
    monkeypatch.setattr(
        R.bridge.Robot,
        "preflight",
        lambda self: {"ok": False, "reasons": ["refused by the test"]},
    )
    install(
        monkeypatch,
        world,
        [
            ("按 [Enter] 续跑", ENTER),
            (CONFIRM, "y"),
            ("按 [Enter] 续跑", "q"),
        ],
    )
    run(world, timeout=6)
    resumed = _resumes(world)
    assert len(resumed) == 1 and resumed[0].to_state == "PREFLIGHT"
    assert not any(e.to_state == "FORWARD_ACTIVE" for e in journal(world))


def test_n_at_the_confirmation_goes_back_to_waiting(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=2)
    O, preflight = _wait_for(world, "policy_error")
    monkeypatch.setattr(O, "_preflight", preflight)
    install(
        monkeypatch,
        world,
        [
            ("按 [Enter] 续跑", ENTER),
            (CONFIRM, "n"),
            ("按 [Enter] 续跑", "q"),  # the wait line is shown again
        ],
    )
    run(world, timeout=6)
    assert _resumes(world) == []
    assert "已取消，继续等待" in world.out.getvalue()
    assert world.out.getvalue().count("策略不可用或出错") == 2


def test_other_waits_keep_the_single_key(tmp_path, monkeypatch):
    """F4 is limited to the three reasons: a scene wait still resumes on one
    Enter and never asks for a confirmation."""
    world = attested(tmp_path, monkeypatch, episodes=2)
    O, preflight = _wait_for(world, "scene_reset_required")
    monkeypatch.setattr(O, "_preflight", preflight)
    install(
        monkeypatch,
        world,
        [
            ("按 [Enter] 续跑", ENTER),
            ("[y/n/u]", YES),
            ("按 [Enter]", ENTER),
            ("[y/n/u]", YES),
        ],
    )
    assert run(world) == "COMPLETED"
    assert CONFIRM not in world.out.getvalue()
    assert len(_resumes(world)) == 1


def test_a_stale_enter_cannot_resume_a_wait_even_without_the_scene_flow(
    tmp_path, monkeypatch
):
    """M6: an Enter already in the buffer when the wait starts is flushed;
    the run keeps waiting for the key typed after the prompt (here q)."""
    world = attested(tmp_path, monkeypatch, episodes=2)
    holder = {}
    install(
        monkeypatch,
        world,
        [
            ("[y/n/u]", YES),
            ("按 [Enter]", ENTER),
            ("[y/n/u]", "n"),
            ("已记录", lambda: holder["keys"].buffer.extend("\n")),
            ("场景需复位", "q"),
        ],
    )
    holder["keys"] = world.keys
    run(world, timeout=6)
    assert _resumes(world) == []
    assert keys_of(world)[-1] == "q"


def test_an_enter_between_the_flush_and_the_confirmation_prompt_is_not_a_confirmation(
    tmp_path, monkeypatch
):
    """M6 (F4 path): a key that arrives after the flush but before the
    prompt is printed can only be an Enter; Enter never confirms."""
    world = attested(tmp_path, monkeypatch, episodes=2)
    O, preflight = _wait_for(world, "operator_stop")
    monkeypatch.setattr(O, "_preflight", preflight)
    install(
        monkeypatch,
        world,
        [("按 [Enter] 续跑", ENTER), (CONFIRM, "q")],
    )
    say = C.Console.say

    def say_with_late_enter(self, text=""):
        if CONFIRM in text:
            world.keys.buffer.append("\n")  # lands after the flush
        return say(self, text)

    monkeypatch.setattr(C.Console, "say", say_with_late_enter)
    run(world, timeout=6)
    assert _resumes(world) == []


# --- mutants that survived (M13, M2d) -------------------------------------------------------------


def test_a_page_resume_of_wait_human_ends_the_terminal_wait(tmp_path, monkeypatch):
    """M13: when the page resumes a WAIT_HUMAN, the terminal stops waiting for
    a key, says so, and leaves raw mode; no key of its own was used."""
    world = attested(tmp_path, monkeypatch, episodes=2)

    def page_resumes():
        orch = world.console["it"].orch
        found = R.control.resume_checked(
            orch,
            "page-resume-1",
            expected_seq=orch.journal.next_seq,
            environment_handled=True,
            health_rechecked=True,
        )
        assert found.ok, found

    install(
        monkeypatch,
        world,
        [
            ("[y/n/u]", YES),
            ("按 [Enter]", ENTER),
            ("[y/n/u]", "n"),
            ("场景需复位", page_resumes),
            ("[y/n/u]", YES),  # the next scene question
        ],
    )
    assert run(world) == "COMPLETED"
    out = world.out.getvalue()
    assert "页面已处理，继续" in out
    assert "n" in keys_of(world) and ENTER not in keys_of(world)[3:]
    assert len(_resumes(world)) == 1
    assert world.keys.depth == 0


def test_closing_the_terminal_transport_leaves_raw_mode_with_a_question_pending():
    """M2d: a run that ends while a scene question is still asked must give
    the terminal back: ``close`` really leaves cbreak."""
    out = Out()
    keys = Keys(out)
    transport = human.TerminalTransport(keys, lambda t: None)
    transport.ask(question(("a",)))
    assert keys.depth == 1  # the question put the terminal in cbreak
    transport.close()
    assert keys.depth == 0


def test_closing_the_console_closes_its_pending_transport():
    out = Out()
    keys = Keys(out)
    console = C.Console(keys, out)
    console.terminal().ask(question(("a",)))
    assert keys.depth == 1
    console.close()
    assert keys.depth == 0


# --- F7: operator-facing words come from console_text ------------------------------------------------


def test_a_failed_journal_note_is_reported_in_the_console_language():
    out = Out()
    console = C.Console(Keys(out), out)
    console.orch = SimpleNamespace(
        journal=SimpleNamespace(
            note=lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
        ),
        _auth=lambda who: who,
    )
    console.note("x_note", "detail")
    assert "日志备注 x_note 未写入" in out.getvalue() and "disk" in out.getvalue()
    console, out = C.Console(Keys(out), out, lang="en"), Out()
    console.out = out
    console.orch = SimpleNamespace(
        journal=SimpleNamespace(
            note=lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
        ),
        _auth=lambda who: who,
    )
    console.note("x_note", "detail")
    assert "journal note x_note not written" in out.getvalue()


def test_the_plain_fault_print_hides_judgement_notes(capsys):
    """F3 again: ``fr3_run.print_fault`` (stderr) printed ``orch.notes``
    verbatim, early_stop_disputed included."""
    orch = SimpleNamespace(
        fault_reason="watchdog_timeout",
        halted=None,
        notes=[
            ("hold_failed", "arm busy"),
            ("early_stop_disputed", "final judgement: success VERDICT-LEAK"),
        ],
    )
    R.print_fault(orch, "/tmp/run")
    err = capsys.readouterr().err
    assert "hold_failed: arm busy" in err
    assert "VERDICT-LEAK" not in err and "early_stop_disputed" not in err


POLICY_ERROR = ("policy_unavailable", "r.1: cold-server: websocket closed (1011)")
LONG_POLICY_ERROR = ("policy_unavailable", "r.2: " + "x" * 400 + "TAIL-CUT")


def test_policy_error_text_is_shown_and_judgement_notes_are_not(capsys):
    """L2-REAL-ERRLOG after F3: the policy/adapter error text is its own
    whitelisted class (cut to 300 characters); judgement notes stay hidden on
    both the console fault summary and the plain ``print_fault``."""
    notes_ = [POLICY_ERROR, *VERDICT_NOTES, LONG_POLICY_ERROR]
    console, out = _noted_console(notes_)
    console.finish()
    orch = SimpleNamespace(fault_reason="policy_error", halted=None, notes=notes_)
    R.print_fault(orch, "/tmp/run")
    err = capsys.readouterr().err
    for text in (out.getvalue(), err):
        assert "websocket closed (1011)" in text
        assert "x" * 290 in text and "TAIL-CUT" not in text
        assert "VERDICT-LEAK" not in text and "early_stop_disputed" not in text
    shown = console._recent_notes(count=10)
    assert [c for c, _ in shown] == ["policy_unavailable", "policy_unavailable"]
    assert all(len(d) <= C.POLICY_ERROR_LIMIT for _, d in shown)


def test_policy_error_text_is_cut_at_300_characters(capsys):
    """The cut is 300 characters exactly, on shown_notes and on stderr."""
    detail = "p" * 300 + "q" * 50
    assert C.POLICY_ERROR_LIMIT == 300
    assert C.shown_notes([("policy_unavailable", detail)]) == [
        ("policy_unavailable", "p" * 300)
    ]
    orch = SimpleNamespace(
        fault_reason="policy_error",
        halted=None,
        notes=[("policy_unavailable", detail)],
    )
    R.print_fault(orch, "/tmp/run")
    err = capsys.readouterr().err
    assert "p" * 300 in err and "q" not in err.split("p" * 300, 1)[1].split("\n")[0]


# --- a failing Console.close does not change the run's result ---------------------------------------


def _dead_console_close(monkeypatch):
    def dead(self, state=None):
        raise OSError("terminal gone")

    monkeypatch.setattr(C.Console, "close", dead)


def test_a_console_close_failure_keeps_the_run_state_and_warns(
    tmp_path, monkeypatch, capsys
):
    world = attested(tmp_path, monkeypatch, episodes=1)
    install(monkeypatch, world, [("[y/n/u]", YES)])
    _dead_console_close(monkeypatch)
    assert run_main(world) == "COMPLETED"  # no OSError out of execute
    err = capsys.readouterr().err
    assert "控制台关闭失败" in err and "terminal gone" in err
    assert "console_close_failed" in notes(world)


def test_a_console_close_failure_does_not_make_main_exit_2(
    tmp_path, monkeypatch, capsys
):
    world = attested(tmp_path, monkeypatch, episodes=1)
    install(monkeypatch, world, [("[y/n/u]", YES)])
    _dead_console_close(monkeypatch)
    original = R.execute

    def real_execute(job, binding):
        job.pop("standalone_inputs", None)  # no run registry in this test
        return original(job, binding, load_modules=lambda b: world.modules)

    code = _main_stopped_by_signal(tmp_path, monkeypatch, world, real_execute)
    assert code == 0, capsys.readouterr().err


def test_the_console_close_warning_is_english_with_the_english_console(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv(console_text.ENV_LANG, "en")
    world = attested(tmp_path, monkeypatch, episodes=1)
    install(monkeypatch, world, [("[y/n/u]", YES)], lang="en")
    _dead_console_close(monkeypatch)
    assert run_main(world) == "COMPLETED"
    err = capsys.readouterr().err
    assert "Console close failed" in err
    assert "控制台" not in err


# --- a dead stderr in the entry's cleanup (review SHOULD 1 and 2) ----------------------------------


class DeadAfterArming(io.StringIO):
    """stderr that is fine until ``armed``, then every write fails as a
    terminal that went away does (EIO)."""

    def __init__(self, log=None):
        super().__init__()
        self.armed = False
        self.log = log if log is not None else []

    def write(self, text):
        if self.armed:
            self.log.append("stderr_write_failed")
            raise OSError(errno.EIO, "Input/output error")
        return super().write(text)


def _spy_releases(monkeypatch, log):
    for owner, name in (
        (R.sm.MotionFence, "revoke"),
        (R.Orchestrator, "close"),
        (fr3_media.Recorder, "close"),
        (R.bridge.Robot, "emergency_hold"),
    ):
        original = getattr(owner, name)

        def spy(self, *a, _o=original, _n=owner.__name__ + "." + name, **kw):
            log.append(_n)
            return _o(self, *a, **kw)

        monkeypatch.setattr(owner, name, spy)


def test_a_dead_stderr_at_fault_locked_still_releases_everything(tmp_path, monkeypatch):
    """SHOULD 1: ``print_fault`` was the first line of the entry's finally;
    a dead stderr (EIO) there skipped the emergency hold, the fence, the
    orchestrator, the recorder and the signal handlers."""
    world = attested(tmp_path, monkeypatch, episodes=1, faults=("home_error",))
    install(monkeypatch, world, [("[y/n/u]", YES)])
    log = []
    stderr = DeadAfterArming(log)
    monkeypatch.setattr(R.sys, "stderr", stderr)
    _spy_releases(monkeypatch, log)
    run_orch = R.Orchestrator.run

    def run_then_kill_stderr(self, *a, **kw):
        try:
            return run_orch(self, *a, **kw)
        finally:
            log.append("run_returned")
            stderr.armed = True

    monkeypatch.setattr(R.Orchestrator, "run", run_then_kill_stderr)
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    assert run_main(world) == "FAULT_LOCKED"
    assert "stderr_write_failed" in log  # the fault print really hit the dead stderr
    after = log[len(log) - 1 - log[::-1].index("run_returned") :]
    assert "Robot.emergency_hold" in after
    assert {"MotionFence.revoke", "Orchestrator.close", "Recorder.close"} <= set(after)
    # N3: the arm is held before the fault summary is printed
    assert after.index("Robot.emergency_hold") < after.index("stderr_write_failed")
    assert {s: signal.getsignal(s) for s in before} == before


def _dead_close_and_stderr(monkeypatch):
    stderr = DeadAfterArming()

    def dead(self, state=None):
        stderr.armed = True  # the tty and stderr went away together
        raise OSError("terminal gone")

    monkeypatch.setattr(C.Console, "close", dead)
    monkeypatch.setattr(R.sys, "stderr", stderr)
    note = Journal.note

    def logged_note(self, code, *a, **kw):
        stderr.log.append("note:" + code)
        return note(self, code, *a, **kw)

    monkeypatch.setattr(Journal, "note", logged_note)
    return stderr


def test_a_dead_console_and_stderr_keep_the_state_and_write_the_note(
    tmp_path, monkeypatch
):
    """SHOULD 2: the warning was printed before the journal note and without
    protection: a dead stderr lost the note and made the run fail."""
    world = attested(tmp_path, monkeypatch, episodes=1)
    install(monkeypatch, world, [("[y/n/u]", YES)])
    stderr = _dead_close_and_stderr(monkeypatch)
    assert run_main(world) == "COMPLETED"
    assert "stderr_write_failed" in stderr.log
    assert "console_close_failed" in notes(world)
    # N3: the note is written before the warning is printed
    assert stderr.log.index("note:console_close_failed") < stderr.log.index(
        "stderr_write_failed"
    )


def test_a_dead_console_and_stderr_do_not_make_main_exit_2(tmp_path, monkeypatch):
    world = attested(tmp_path, monkeypatch, episodes=1)
    install(monkeypatch, world, [("[y/n/u]", YES)])
    stderr = _dead_close_and_stderr(monkeypatch)
    original = R.execute

    def real_execute(job, binding):
        job.pop("standalone_inputs", None)  # no run registry in this test
        return original(job, binding, load_modules=lambda b: world.modules)

    assert _main_stopped_by_signal(tmp_path, monkeypatch, world, real_execute) == 0
    assert "stderr_write_failed" in stderr.log
    assert "console_close_failed" in notes(world)


def test_policy_error_text_cannot_break_the_terminal_line(capsys):
    """Control characters (\\n, \\r, ESC, DEL, ...) become spaces before the
    cut: the error text stays on one line and carries no ANSI escape."""
    raw = "r.3: boom\n\x1b[2Jcleared\rover\x00\x7fend"
    detail = C.shown_notes([("policy_unavailable", raw)])[0][1]
    assert not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in detail)
    orch = SimpleNamespace(
        fault_reason="policy_error",
        halted=None,
        notes=[("policy_unavailable", raw)],
    )
    R.print_fault(orch, "/tmp/run")
    err = capsys.readouterr().err
    assert "\x1b" not in err and "\r" not in err
    lines = [line for line in err.splitlines() if "boom" in line]
    assert len(lines) == 1 and "[2Jcleared over" in lines[0] and "end" in lines[0]
