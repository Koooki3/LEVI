"""The operator's verdict on the forward episode under way (terminal D's
``s``/``f``/``d``, L2-AERI-OPERATOR-EARLY-STOP): it ends that episode only,
through the ordinary hold, quiesce, seal and home, and the run goes on. The
verdict is kept apart from the model's judgements (a journal note and the
operator's label file); a ``d`` is excluded from the statistics. In-process
fakes only: no robot, socket, GPU or service."""

import json
import threading

import pytest
from aeri_harness import build, committed, config, restart, results

from levi.automatic import metrics
from levi.automatic import state_machine as sm
from levi.automatic.journal import Journal
from levi.automatic.orchestrator import (
    VERDICT_DROPPED_NOTE,
    VERDICT_NOT_LABELLED_NOTE,
    VERDICT_NOTE,
    ConfigError,
)

LABEL_OF = {"success": "success", "failure": "failure", "discard": "discarded"}


def at_call(r, n, action):
    """Run ``action()`` just before the robot's ``n``-th policy step."""
    original = r.robot.step
    calls = {"n": 0}

    def step(act, *, token):
        calls["n"] += 1
        if calls["n"] == n:
            action()
        return original(act, token=token)

    r.robot.step = step


def notes(orch, code):
    return [
        e.note.detail
        for e in orch.journal.events
        if e.record == "note" and e.note.code == code
    ]


def two_episodes():
    return config(episodes=2)


@pytest.mark.parametrize("label", ["success", "failure", "discard"])
def test_a_verdict_ends_this_episode_only_and_the_run_goes_on(tmp_path, label):
    r = build(tmp_path, cfg=two_episodes())
    answers = []
    at_call(r, 3, lambda: answers.append(r.orch.episode_verdict("verdict-1", label)))
    assert r.orch.run() == "COMPLETED"
    assert answers[0].ok and answers[0].code == "verdict_requested"
    path = committed(r.orch)
    first = path.index(("FORWARD_ACTIVE", "FORWARD_STOPPING", "operator_verdict"))
    # The ordinary end of an episode: hold, quiesce, seal, home, next round.
    assert path[first : first + 4] == [
        ("FORWARD_ACTIVE", "FORWARD_STOPPING", "operator_verdict"),
        ("FORWARD_STOPPING", "FORWARD_FINALIZE", "operator_verdict"),
        ("FORWARD_FINALIZE", "ROBOT_HOME", "operator_verdict"),
        ("ROBOT_HOME", "SCENE_ASSESS", "robot_home_reached"),
    ]
    # Not the whole run: no wait for a person, the second round ran.
    assert not any(to == "WAIT_HUMAN" for _, to, _ in path)
    # s and f count as rounds; a discarded round is replaced by one more.
    rounds = 3 if label == "discard" else 2
    assert [to for _, to, _ in path].count("FORWARD_ACTIVE") == rounds
    assert r.orch.completed_forward == 2
    assert r.orch.discarded_forward == (1 if label == "discard" else 0)
    assert path[-1] == ("SCENE_ASSESS", "COMPLETED", "run_completed")
    assert sm.check_journal(r.orch.journal.events) == []
    stopping = next(
        e
        for e in r.orch.journal.events
        if e.record == "committed" and e.reason == "operator_verdict"
    )
    assert stopping.authority.principal_kind == "operator"
    assert stopping.authority.command_id == "verdict-1"
    # The episode result is still the run's own verdict (the model's).
    found = results(r.orch)[0]
    assert found.stop_reason == "operator_verdict"
    assert found.label_kind == "autonomous_verdict"
    # The person's verdict: a journal note with every field ...
    [detail] = notes(r.orch, VERDICT_NOTE)
    value = json.loads(detail)
    assert value["label"] == label and value["source"] == "terminal"
    assert value["episode_id"] == stopping.episode_id
    assert value["step"] == 2 and value["frame_index"] == 1
    assert value["taken_step"] == 3
    assert isinstance(value["wall_ns"], int) and value["wall_ns"] > 0
    assert value["command_id"] == "verdict-1"
    # ... and the operator's label, in its own file, not in the journal.
    lines = metrics.LabelStore(r.directory).lines("operator_label")
    assert [(x["episode_id"], x["value"]) for x in lines] == [
        (stopping.episode_id, LABEL_OF[label])
    ]
    assert "operator_verdict" in lines[0]["note"]


def test_a_discarded_episode_is_left_out_of_the_statistics(tmp_path):
    r = build(tmp_path, cfg=two_episodes())
    at_call(r, 3, lambda: r.orch.episode_verdict("verdict-d", "discard"))
    assert r.orch.run() == "COMPLETED"
    events = Journal.read(r.directory).events
    store = metrics.LabelStore(r.directory)
    report = metrics.report(events, labels=store)
    episode = results(r.orch)[0]
    first = next(
        e.episode_id for e in events if e.record == "committed" and e.episode_result
    )
    assert episode.stop_reason == "operator_verdict"
    summary = report["operator_verdicts"]
    assert summary["labels"] == {"success": 0, "failure": 0, "discard": 1}
    assert summary["excluded"] == [first]
    assert summary["verdicts"][0]["excluded"] is True
    # Not a round: out of the counts, the autonomous rate, the agreement and
    # the truth; three rounds ran so that two valid ones remain.
    assert first not in store.truth()
    assert len(results(r.orch)) == 3
    assert report["autonomous"]["forward_episodes"] == 2
    assert report["autonomous"]["autonomous_success_rate"]["of"] == 2
    assert report["agreement"]["total"]["episodes"] == 2
    assert report["agreement"]["total"]["discarded"] == 0
    assert report["early_termination"]["labelled"] == 0
    # A person ended it: never in the detector's eligible episodes.
    assert metrics.ended_by("operator_verdict") == "operator_stop"


def test_success_and_failure_verdicts_are_truth_labels(tmp_path):
    r = build(tmp_path, cfg=two_episodes())
    at_call(r, 3, lambda: r.orch.episode_verdict("verdict-f", "failure"))
    assert r.orch.run() == "COMPLETED"
    store = metrics.LabelStore(r.directory)
    first = results(r.orch)[0]
    assert first.stop_reason == "operator_verdict"
    truth = store.truth()
    assert list(truth.values()) == ["failure"]
    report = metrics.report(Journal.read(r.directory).events, labels=store)
    assert report["operator_verdicts"]["labels"]["failure"] == 1
    assert report["operator_verdicts"]["excluded"] == []


def test_without_a_key_the_run_is_unchanged(tmp_path):
    plain = build(tmp_path / "plain")
    assert plain.orch.run() == "COMPLETED"
    path = [(a, b) for a, b, _ in committed(plain.orch)]
    assert path[:7] == [
        ("PREFLIGHT", "VERIFY_INITIAL"),
        ("VERIFY_INITIAL", "FORWARD_ACTIVE"),
        ("FORWARD_ACTIVE", "FORWARD_STOPPING"),
        ("FORWARD_STOPPING", "FORWARD_FINALIZE"),
        ("FORWARD_FINALIZE", "ROBOT_HOME"),
        ("ROBOT_HOME", "SCENE_ASSESS"),
        ("SCENE_ASSESS", "FORWARD_ACTIVE"),
    ]
    assert [x.stop_reason for x in results(plain.orch)] == [
        "goal_verified",
        "goal_verified",
    ]
    assert notes(plain.orch, VERDICT_NOTE) == []
    assert notes(plain.orch, VERDICT_DROPPED_NOTE) == []
    assert not (plain.directory / "labels" / "operator_label.jsonl").exists()
    # The same rig, run again with an unused verdict path, says the same.
    again = build(tmp_path / "again")
    assert again.orch.run() == "COMPLETED"
    assert committed(again.orch) == committed(plain.orch)


@pytest.mark.parametrize(
    ("branch", "label"),
    [
        ("home_after_stop_false", "failure"),
        ("before_the_first_step", "success"),
        ("before_the_first_step", "failure"),
        ("before_the_first_step", "discard"),
    ],
)
def test_a_verdict_homes_where_a_stop_would_stay_put(tmp_path, branch, label):
    """A stop leaves the arm where it is when the episode ran no step or
    ``home_after_operator_stop`` is false (``_stays_put``); a verdict is not
    a stop: in both cases the arm goes home and the next round runs."""
    if branch == "home_after_stop_false":
        r = build(tmp_path, cfg=config(episodes=2, home_after_operator_stop=False))
        at_call(r, 3, lambda: r.orch.episode_verdict("verdict-1", label))
    else:
        r = build(tmp_path, cfg=two_episodes())
        original, calls = r.orch._take_verdict, {"n": 0}

        def take(ctx):
            # Registered as the first step begins: the episode ran no step.
            calls["n"] += 1
            if calls["n"] == 1:
                assert r.orch.episode_verdict("verdict-1", label).ok
            return original(ctx)

        r.orch._take_verdict = take
    assert r.orch.run() == "COMPLETED"
    path = committed(r.orch)
    first = path.index(("FORWARD_ACTIVE", "FORWARD_STOPPING", "operator_verdict"))
    rest = path[first:]
    home = next(i for i, (_, to, _) in enumerate(rest) if to == "ROBOT_HOME")
    assert rest[home + 1] == ("ROBOT_HOME", "SCENE_ASSESS", "robot_home_reached")
    assert not any(to == "WAIT_HUMAN" for _, to, _ in path)
    # An episode that ran no step is aborted, not a round: one more runs.
    rounds = 3 if branch == "before_the_first_step" else 2
    assert [to for _, to, _ in path].count("FORWARD_ACTIVE") == rounds
    assert r.orch.completed_forward == 2
    if branch == "home_after_stop_false":
        assert results(r.orch)[0].stop_reason == "operator_verdict"
    else:
        # Taken at the first step's check: no policy step ran.
        [detail] = notes(r.orch, VERDICT_NOTE)
        value = json.loads(detail)
        assert value["taken_step"] == 0
        # No rollout to label: no operator label may enter the truth.
        store = metrics.LabelStore(r.directory)
        lines = store.lines("operator_label")
        assert value["episode_id"] not in {x["episode_id"] for x in lines}
        assert value["episode_id"] not in store.truth()
        [why] = notes(r.orch, VERDICT_NOT_LABELLED_NOTE)
        assert why.startswith(value["episode_id"] + ": no rollout")
        # Not a round, not a replaced discard, nothing beyond the limit.
        assert r.orch.discarded_forward == 0
        assert r.orch.discarded_beyond_limit == 0
        report = metrics.report(Journal.read(r.directory).events, labels=store)
        assert report["discarded_beyond_limit"] == 0
        excluded = report["operator_verdicts"]["excluded"]
        assert excluded == ([value["episode_id"]] if label == "discard" else [])
    assert sm.check_journal(r.orch.journal.events) == []


@pytest.mark.xfail(
    strict=True,
    reason="L2-AERI-LABEL-ABORTED-ROUND: metrics counts a round aborted before "
    "the first step",
)
@pytest.mark.parametrize("label", ["success", "failure"])
def test_a_round_aborted_before_the_first_step_is_not_in_the_statistics(
    tmp_path, label
):
    """The run does not count a round that ran no step (one more replaces
    it); the metrics should not either. Known gap: they still do."""
    r = build(tmp_path, cfg=two_episodes())
    original, calls = r.orch._take_verdict, {"n": 0}

    def take(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            assert r.orch.episode_verdict("verdict-1", label).ok
        return original(ctx)

    r.orch._take_verdict = take
    assert r.orch.run() == "COMPLETED"
    store = metrics.LabelStore(r.directory)
    report = metrics.report(Journal.read(r.directory).events, labels=store)
    assert report["autonomous"]["forward_episodes"] == r.orch.completed_forward == 2
    assert report["autonomous"]["autonomous_success_rate"]["of"] == 2


def test_a_verdict_is_refused_outside_a_forward_episode(tmp_path):
    r = build(tmp_path, cfg=config(episodes=1))
    # Before the run (PREFLIGHT): nothing runs, nothing is registered.
    found = r.orch.episode_verdict("verdict-early", "success")
    assert not found.ok and found.code == "not_in_forward"
    seen = []
    original = r.robot.home

    def home(*, token):
        # While the arm goes home after the episode: refused, nothing kept.
        seen.append(r.orch.episode_verdict("verdict-home", "failure"))
        return original(token=token)

    r.robot.home = home
    assert r.orch.run() == "COMPLETED"
    assert seen and all(not x.ok and x.code == "not_in_forward" for x in seen)
    assert notes(r.orch, VERDICT_NOTE) == []
    assert [x.stop_reason for x in results(r.orch)] == ["goal_verified"]
    # After the run.
    found = r.orch.episode_verdict("verdict-late", "success")
    assert not found.ok and found.code == "not_in_forward"


def test_a_stop_wins_over_a_verdict_and_ends_the_run(tmp_path):
    r = build(tmp_path, cfg=two_episodes())
    answers = []

    def both():
        answers.append(r.orch.episode_verdict("verdict-1", "success"))
        r.orch.stop("stop-1")

    at_call(r, 3, both)
    assert r.orch.run() == "WAIT_HUMAN"
    assert answers[0].ok
    path = committed(r.orch)
    assert ("FORWARD_ACTIVE", "FORWARD_STOPPING", "operator_stop") in path
    assert not any(reason == "operator_verdict" for *_, reason in path)
    assert notes(r.orch, VERDICT_NOTE) == []
    assert len(notes(r.orch, VERDICT_DROPPED_NOTE)) == 1
    # and a verdict while a stop is pending is refused outright
    r2 = build(tmp_path / "second", cfg=two_episodes())
    seen = []

    def stop_then_verdict():
        r2.orch.stop("stop-2")
        seen.append(r2.orch.episode_verdict("verdict-2", "failure"))

    at_call(r2, 3, stop_then_verdict)
    assert r2.orch.run() == "WAIT_HUMAN"
    assert not seen[0].ok and seen[0].code == "stop_pending"


def test_the_latch_keeps_its_priority_over_a_verdict(tmp_path):
    r = build(tmp_path, cfg=two_episodes())

    def latch_and_verdict():
        assert r.orch.episode_verdict("verdict-1", "success").ok
        r.robot.latched = "reflex"

    at_call(r, 3, latch_and_verdict)
    assert r.orch.run() == "FAULT_LOCKED"
    path = committed(r.orch)
    assert path[-1][1:] == ("FAULT_LOCKED", "safety_stop")
    assert not any(reason == "operator_verdict" for *_, reason in path)
    assert notes(r.orch, VERDICT_NOTE) == []


def test_a_latch_and_a_verdict_at_the_same_check_lock_the_run(tmp_path):
    """The verdict is registered and the guard latches after a step, so both
    are pending at the same next check: the latch wins, the verdict is never
    taken and no operator label is written."""
    r = build(tmp_path, cfg=two_episodes())
    original, calls = r.robot.step, {"n": 0}

    def step(act, *, token):
        out = original(act, token=token)
        calls["n"] += 1
        if calls["n"] == 3:
            assert r.orch.episode_verdict("verdict-1", "success").ok
            r.robot.latched = "reflex"
        return out

    r.robot.step = step
    assert r.orch.run() == "FAULT_LOCKED"
    path = committed(r.orch)
    assert path[-1][1:] == ("FAULT_LOCKED", "safety_stop")
    assert not any(reason == "operator_verdict" for *_, reason in path)
    assert notes(r.orch, VERDICT_NOTE) == []
    assert metrics.LabelStore(r.directory).lines("operator_label") == []


def test_one_verdict_per_episode_and_bad_requests_are_refused(tmp_path):
    r = build(tmp_path, cfg=two_episodes())
    seen = {}

    def press():
        seen["first"] = r.orch.episode_verdict("verdict-1", "success")
        seen["again"] = r.orch.episode_verdict("verdict-1", "success")
        seen["second"] = r.orch.episode_verdict("verdict-2", "failure")
        seen["label"] = r.orch.episode_verdict("verdict-3", "unclear")
        seen["page"] = r.orch.episode_verdict("verdict-4", "success", source="page")
        seen["id"] = r.orch.episode_verdict("../x", "success")

    at_call(r, 3, press)
    assert r.orch.run() == "COMPLETED"
    assert seen["first"].ok and seen["first"].code == "verdict_requested"
    assert seen["again"].ok and seen["again"].repeated
    assert (seen["second"].ok, seen["second"].code) == (False, "verdict_pending")
    for name in ("label", "page", "id"):
        assert (seen[name].ok, seen[name].code) == (False, "invalid"), name
    assert len(notes(r.orch, VERDICT_NOTE)) == 1
    # After the run nothing is taken.
    assert r.orch.episode_verdict("stop-x", "success").code == "not_in_forward"


def test_event_pause_judge_runs_take_no_verdict(tmp_path):
    r = build(tmp_path, cfg=config(episodes=1))
    r.orch.intervention = object()  # what an event_pause_judge run carries
    try:
        found = r.orch.episode_verdict("verdict-1", "success")
    finally:
        r.orch.intervention = None
    assert not found.ok and found.code == "not_supported"


def verdict_in_rounds(r, rounds, label="discard"):
    """A verdict at the third step of each listed forward round."""
    original = r.robot.step

    def step(act, *, token):
        ctx = r.orch.ctx
        number = r.orch.numbers["forward"]
        forward = ctx is not None and ctx.role == "forward"
        if forward and ctx.steps_done == 2 and number in rounds:
            r.orch.episode_verdict(f"verdict-{number}", label)
        return original(act, token=token)

    r.robot.step = step


def test_a_discarded_round_is_replaced_and_the_run_keeps_n_valid_rounds(tmp_path):
    r = build(tmp_path, cfg=two_episodes())
    verdict_in_rounds(r, {1})
    assert r.orch.run() == "COMPLETED"
    ended = results(r.orch)
    assert ended[0].stop_reason == "operator_verdict"
    assert len(ended) == 3  # one more round replaced the discarded one
    assert (r.orch.completed_forward, r.orch.discarded_forward) == (2, 1)
    assert notes(r.orch, "discard_limit_reached") == []
    # A restart reads the same counts back from the journal.
    again = restart(r)
    assert (again.orch.completed_forward, again.orch.discarded_forward) == (2, 1)


def test_discards_beyond_the_limit_are_not_replaced_and_the_run_ends(tmp_path):
    r = build(tmp_path, cfg=config(episodes=2, max_discarded_rounds=1))
    verdict_in_rounds(r, {1, 2, 3})
    assert r.orch.run() == "COMPLETED"
    ended = results(r.orch)
    # Round 1 is replaced (limit 1); rounds 2 and 3 are discarded too and
    # not replaced: three rounds in all, the run completes as usual.
    assert [x.stop_reason for x in ended] == ["operator_verdict"] * 3
    assert (r.orch.completed_forward, r.orch.discarded_forward) == (2, 1)
    assert r.orch.discarded_beyond_limit == 2
    limit_notes = notes(r.orch, "discard_limit_reached")
    assert len(limit_notes) == 2
    assert all("not replaced (limit 1)" in x for x in limit_notes)
    assert all("fewer valid rounds" in x for x in limit_notes)
    assert committed(r.orch)[-1] == ("SCENE_ASSESS", "COMPLETED", "run_completed")
    again = restart(r)
    assert (
        again.orch.completed_forward,
        again.orch.discarded_forward,
        again.orch.discarded_beyond_limit,
    ) == (2, 1, 2)
    # Every discard stays out of the statistics: valid rounds = N - beyond.
    report = metrics.report(
        Journal.read(r.directory).events, labels=metrics.LabelStore(r.directory)
    )
    assert len(report["operator_verdicts"]["excluded"]) == 3
    assert report["discarded_beyond_limit"] == 2
    assert report["operator_verdicts"]["discarded_beyond_limit"] == 2
    valid = report["autonomous"]["forward_episodes"]
    assert valid == r.config.episodes - report["discarded_beyond_limit"] == 0
    assert metrics.LabelStore(r.directory).truth() == {}


def test_the_default_limit_is_the_number_of_rounds(tmp_path):
    r = build(tmp_path, cfg=two_episodes())
    assert r.orch.discard_limit == 2
    verdict_in_rounds(r, {1, 2, 3, 4, 5})
    assert r.orch.run() == "COMPLETED"
    # two replaced, then two more discarded rounds count: 4 rounds in all
    assert len(results(r.orch)) == 4
    assert (r.orch.completed_forward, r.orch.discarded_forward) == (2, 2)
    with pytest.raises(ConfigError):
        config(episodes=2, max_discarded_rounds=-1)


def test_success_and_failure_still_count_as_rounds(tmp_path):
    for label in ("success", "failure"):
        r = build(tmp_path / label, cfg=two_episodes())
        verdict_in_rounds(r, {1, 2}, label=label)
        assert r.orch.run() == "COMPLETED"
        assert len(results(r.orch)) == 2
        assert (r.orch.completed_forward, r.orch.discarded_forward) == (2, 0)
        assert notes(r.orch, "discard_limit_reached") == []


def test_valid_rounds_are_n_minus_the_discards_beyond_the_limit(tmp_path):
    r = build(tmp_path, cfg=config(episodes=3, max_discarded_rounds=1))
    verdict_in_rounds(r, {1, 2})
    assert r.orch.run() == "COMPLETED"
    # round 1 replaced, round 2 not: 4 rounds ran, 2 of them valid
    assert len(results(r.orch)) == 4
    report = metrics.report(
        Journal.read(r.directory).events, labels=metrics.LabelStore(r.directory)
    )
    assert report["discarded_beyond_limit"] == 1
    assert report["autonomous"]["forward_episodes"] == 3 - 1
    assert report["autonomous"]["autonomous_success_rate"]["of"] == 2


def test_replay_after_a_replaced_discard_matches_the_live_counts(tmp_path):
    r = build(tmp_path, cfg=two_episodes())
    verdict_in_rounds(r, {1})
    assert r.orch.run() == "COMPLETED"
    live = (
        r.orch.completed_forward,
        r.orch.discarded_forward,
        r.orch.discarded_beyond_limit,
    )
    assert live == (2, 1, 0)
    again = restart(r)  # a new Orchestrator rebuilt from the journal alone
    assert (
        again.orch.completed_forward,
        again.orch.discarded_forward,
        again.orch.discarded_beyond_limit,
    ) == live


def test_replay_at_the_discard_limit_matches_the_live_counts(tmp_path):
    """max_discarded_rounds=1 and two discards: the first is replaced, the
    second is beyond the limit; a replay from the journal alone counts both
    the same way the live run did (the limit's boundary)."""
    r = build(tmp_path, cfg=config(episodes=2, max_discarded_rounds=1))
    verdict_in_rounds(r, {1, 2})
    assert r.orch.run() == "COMPLETED"
    live = (
        r.orch.completed_forward,
        r.orch.discarded_forward,
        r.orch.discarded_beyond_limit,
    )
    assert live[1:] == (1, 1)
    again = restart(r)
    assert (
        again.orch.completed_forward,
        again.orch.discarded_forward,
        again.orch.discarded_beyond_limit,
    ) == live


def test_a_discard_beyond_the_limit_then_a_stop_and_a_resume(tmp_path):
    r = build(tmp_path, cfg=config(episodes=3, max_discarded_rounds=1))
    verdict_in_rounds(r, {1, 2})
    original = r.robot.step

    def step(act, *, token):
        ctx = r.orch.ctx
        if (
            ctx is not None
            and ctx.role == "forward"
            and ctx.steps_done == 2
            and r.orch.numbers["forward"] == 3
        ):
            r.orch.stop("stop-3")
        return original(act, token=token)

    r.robot.step = step
    assert r.orch.run() == "WAIT_HUMAN"
    live = (
        r.orch.completed_forward,
        r.orch.discarded_forward,
        r.orch.discarded_beyond_limit,
    )
    assert live == (2, 1, 1)
    again = restart(r)
    assert (
        again.orch.completed_forward,
        again.orch.discarded_forward,
        again.orch.discarded_beyond_limit,
    ) == live
    found = again.orch.resume(
        "resume-1",
        expected_seq=again.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )
    assert found.ok
    assert again.orch.run() == "COMPLETED"
    assert (again.orch.completed_forward, again.orch.discarded_forward) == (3, 1)
    report = metrics.report(
        Journal.read(again.directory).events,
        labels=metrics.LabelStore(again.directory),
    )
    assert report["discarded_beyond_limit"] == 1
    assert report["autonomous"]["forward_episodes"] == 3 - 1


def test_a_verdict_left_by_a_latch_does_not_block_the_next_round(tmp_path):
    """M2: the fault that ends the episode clears its registered verdict;
    after the resume the next round's first verdict is taken."""
    r = build(tmp_path, cfg=two_episodes())
    seen = []

    def latch_with_verdict():
        seen.append(r.orch.episode_verdict("verdict-1", "success"))
        r.robot.latched = "reflex"

    at_call(r, 3, latch_with_verdict)
    assert r.orch.run() == "FAULT_LOCKED"
    assert seen[0].ok and r.orch._verdict is None
    dropped = notes(r.orch, VERDICT_DROPPED_NOTE)
    assert len(dropped) == 1 and "locked" in dropped[0]
    r.robot.latched = None
    found = r.orch.resume(
        "resume-1",
        expected_seq=r.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )
    assert found.ok
    later = []
    original = r.robot.step

    def step(act, *, token):
        ctx = r.orch.ctx
        forward = ctx is not None and ctx.role == "forward"
        if forward and ctx.steps_done == 2 and not later:
            later.append(r.orch.episode_verdict("verdict-2", "failure"))
        return original(act, token=token)

    r.robot.step = step
    r.orch.run()
    assert later and later[0].ok and later[0].code == "verdict_requested"
    [taken] = [json.loads(x) for x in notes(r.orch, VERDICT_NOTE)]
    assert taken["command_id"] == "verdict-2" and taken["label"] == "failure"


def test_a_verdict_after_the_loops_last_check_is_dropped(tmp_path):
    """A key that lands while the episode is already being held (after the
    loop's last check) is dropped by ``_finish_forward``, never kept."""
    r = build(tmp_path, cfg=config(episodes=1))
    original = r.robot.hold
    seen = []

    def hold():
        if not seen and r.orch.state == "FORWARD_ACTIVE":
            seen.append(r.orch.episode_verdict("verdict-late", "success"))
        return original()

    r.robot.hold = hold
    assert r.orch.run() == "COMPLETED"
    assert seen and seen[0].ok
    assert notes(r.orch, VERDICT_NOTE) == []
    [dropped] = notes(r.orch, VERDICT_DROPPED_NOTE)
    assert "had ended" in dropped
    assert r.orch._verdict is None
    assert [x.stop_reason for x in results(r.orch)] != ["operator_verdict"]


def test_a_page_source_is_refused(tmp_path):
    """Only the terminal is a source of a verdict."""
    r = build(tmp_path, cfg=config(episodes=1))
    seen = []
    at_call(
        r,
        3,
        lambda: seen.append(
            r.orch.episode_verdict("verdict-page", "success", source="page")
        ),
    )
    assert r.orch.run() == "COMPLETED"
    assert (seen[0].ok, seen[0].code) == (False, "invalid")
    assert notes(r.orch, VERDICT_NOTE) == []


def test_an_inbox_episode_verdict_is_refused_and_changes_nothing(tmp_path, monkeypatch):
    """A hand-written inbox file of kind episode_verdict: the runner answers
    terminal_only and the run is untouched."""
    from types import SimpleNamespace

    from levi.automatic import control, launch

    monkeypatch.setenv(launch.HOME_ENV, str(tmp_path / "aeri-home"))
    r = build(tmp_path, cfg=config(episodes=1))
    runner = SimpleNamespace(
        run_dir=r.directory,
        run_id=r.config.run_id,
        orch=r.orch,
        path=None,
        sigterm=threading.Event(),
        wake=threading.Event(),
        started=1,
    )
    pump = control.CommandPump(runner)
    before = (r.orch.state, r.orch.journal.next_seq)
    inbox = control.ensure_folders(r.directory)["inbox"]
    value = {
        "schema": control.COMMAND_SCHEMA,
        "command_id": "verdict-inbox",
        "kind": "episode_verdict",
        "principal_id": "operator",
        "issued_wall_ns": 1,
        "label": "success",
    }
    (inbox / "verdict-inbox.json").write_text(json.dumps(value))
    pump.tick()
    found = control.read_result(r.directory, "verdict-inbox")
    assert found["ok"] is False and found["code"] == "terminal_only"
    assert r.orch.state == before[0] and r.orch._verdict is None
    # only the audit note of the refused command was added, no transition
    assert not any(e.record == "committed" for e in r.orch.journal.events[before[1] :])


def test_a_verdict_in_the_gap_before_the_fault_commit_is_cleared(tmp_path):
    """A verdict registered while the fault's hold runs (FAULT_LOCKED not
    committed yet, the state still FORWARD_ACTIVE) is cleared once the lock
    is committed: after the resume the next round's first verdict is taken,
    never ``verdict_pending``."""
    r = build(tmp_path, cfg=two_episodes())
    at_call(r, 3, lambda: setattr(r.robot, "latched", "reflex"))
    original_hold = r.robot.hold
    gap = []

    def hold():
        if not gap and r.orch.fault_reason and r.orch.state == "FORWARD_ACTIVE":
            gap.append(r.orch.episode_verdict("verdict-gap", "success"))
        return original_hold()

    r.robot.hold = hold
    assert r.orch.run() == "FAULT_LOCKED"
    assert gap and gap[0].ok and gap[0].code == "verdict_requested"
    assert r.orch._verdict is None
    assert any("locked" in x for x in notes(r.orch, VERDICT_DROPPED_NOTE))
    r.robot.latched = None
    r.robot.hold = original_hold
    found = r.orch.resume(
        "resume-1",
        expected_seq=r.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )
    assert found.ok
    later = []
    original = r.robot.step

    def step(act, *, token):
        ctx = r.orch.ctx
        forward = ctx is not None and ctx.role == "forward"
        if forward and ctx.steps_done == 2 and not later:
            later.append(r.orch.episode_verdict("verdict-next", "failure"))
        return original(act, token=token)

    r.robot.step = step
    r.orch.run()
    assert later and later[0].ok and later[0].code == "verdict_requested"
    taken = [json.loads(x)["command_id"] for x in notes(r.orch, VERDICT_NOTE)]
    assert taken == ["verdict-next"]
