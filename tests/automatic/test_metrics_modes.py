"""Metrics comparable across reset modes (T-CL-05, levi/automatic/metrics.py,
design X2 §1.2): planned and unplanned interventions, the turnaround between
episodes and its parts, time and person minutes per valid episode, the
scene decisions a person made, and the report head. Hand-computed values on
synthetic journals and on fake-driven runs of both modes."""

import json
from types import SimpleNamespace

import pytest
from aeri_harness import RUN, config, fake
from test_aeri_orchestrator import no_network  # noqa: F401
from test_aeri_recorder import FOLDERS, build_run

from levi.automatic import metrics as M
from levi.automatic.adapters import events as ev
from levi.automatic.recorder import MANIFEST

DOMAIN = "host-mono:test"
MS = 1_000_000


def ep(n, role="forward"):
    return f"{RUN}.{role}.{n:04d}"


class Journal:
    """Committed lines of a synthetic run journal, one call per move;
    ``at`` in ms on one clock unless ``domain`` says otherwise."""

    def __init__(self):
        self.events = [
            SimpleNamespace(
                record="run_header",
                header=None,
                from_state=None,
                to_state=None,
                reason=None,
                episode_id=None,
                episode_result=None,
                mono_ns=0,
                clock_domain=DOMAIN,
            )
        ]

    def move(self, at, a, b, reason, *, episode=None, sealed=None, domain=DOMAIN):
        result = None
        if sealed is not None:
            result = SimpleNamespace(
                task_outcome="success",
                stop_reason="goal_verified",
                goal_verification="verified",
                scene_reset="unknown",
                rollout=SimpleNamespace(sealed=sealed),
            )
        self.events.append(
            SimpleNamespace(
                record="committed",
                from_state=a,
                to_state=b,
                reason=reason,
                episode_id=episode,
                episode_result=result,
                mono_ns=at * MS,
                clock_domain=domain,
            )
        )
        return self

    def forward(self, at, n, *, sealed="complete", length=1000):
        """An episode from its scene-ready start to its home reached."""
        e = ep(n)
        self.move(
            at, self.events[-1].to_state, "FORWARD_ACTIVE", "scene_ready", episode=e
        )
        self.move(
            at + length,
            "FORWARD_ACTIVE",
            "FORWARD_STOPPING",
            "goal_verified",
            episode=e,
        )
        self.move(
            at + length,
            "FORWARD_STOPPING",
            "FORWARD_FINALIZE",
            "goal_verified",
            episode=e,
        )
        self.move(
            at + length + 100,
            "FORWARD_FINALIZE",
            "ROBOT_HOME",
            "goal_verified",
            episode=e,
            sealed=sealed,
        )
        return self.move(
            at + length + 200,
            "ROBOT_HOME",
            "SCENE_ASSESS",
            "robot_home_reached",
            episode=e,
        )


def human_run():
    """human_assisted, 3 episodes; after episode 1 the scene needs a person
    (planned, 60 s); after episode 2 the scene is ready; then a fault
    (unplanned, 30 s) before episode 3 ends the window in a person's hands."""
    j = Journal()
    j.move(0, "PREFLIGHT", "VERIFY_INITIAL", "preflight_passed")
    j.forward(300, 1)  # home reached at 1500
    j.move(1800, "SCENE_ASSESS", "WAIT_HUMAN", "scene_reset_required")
    j.move(61_800, "WAIT_HUMAN", "PREFLIGHT", "human_resumed")
    j.move(61_900, "PREFLIGHT", "VERIFY_INITIAL", "preflight_passed")
    j.forward(62_200, 2)  # home reached at 63400
    j.move(63_700, "SCENE_ASSESS", "FAULT_LOCKED", "safety_stop")
    j.move(93_700, "FAULT_LOCKED", "PREFLIGHT", "human_resumed")
    j.move(93_800, "PREFLIGHT", "VERIFY_INITIAL", "preflight_passed")
    j.forward(94_100, 3)  # home reached at 95300
    j.move(95_300, "SCENE_ASSESS", "COMPLETED", "run_completed")
    return j


# --- hand-computed, synthetic journals -----------------------------------------------------------


def test_planned_and_unplanned_interventions_and_the_runs_without_a_person():
    events = human_run().events
    found = M.automation(events, "human_assisted")
    assert found["interventions"] == 2
    assert (found["planned"], found["unplanned"]) == (1, 1)
    assert found["planned_by_reason"] == {"scene_reset_required": 1}
    assert found["unplanned_by_reason"] == {"safety_stop": 1}
    # Episodes 1 | 2 | 3: any person splits after 1 and after 2; an
    # unplanned one only after 2.
    assert found["longest_run_without_any_human"] == 1
    assert found["longest_run_without_intervention"] == 1
    assert found["longest_run_without_unplanned"] == 2
    assert found["current_run_without_unplanned"] == 1
    assert found["person_ms"] == {
        "planned": 60_000,
        "unplanned": 30_000,
        "total": 90_000,
        "unmeasured": 0,
        "open_waits": 0,
    }
    # 1 of 2 unplanned: Wilson 95 % [0.095, 0.905].
    assert found["unplanned_share"] == {
        "n": 1,
        "of": 2,
        "rate": 0.5,
        "wilson95": [0.095, 0.905],
    }
    # The same journal under the other mode or an unknown one: all unplanned.
    for mode in ("single_reset_policy", None):
        other = M.automation(events, mode)
        assert (other["planned"], other["unplanned"]) == (0, 2)
        assert other["longest_run_without_unplanned"] == 1


def test_the_turnaround_is_split_by_state_and_the_parts_add_up():
    found = M.turnaround(human_run().events)
    # Window 1: home 1500 -> start 62200 = 60700: scene 300, person 60000,
    # verify 100 + 300. Window 2: home 63400 -> start 94100 = 30700: scene
    # 300, person 30000, verify 400. The last home ends the run.
    assert found["n"] == 2
    assert found["turnaround_ms"] == {
        "n": 2,
        "total": 91_400,
        "mean": 45_700.0,
        "p50": 45_700.0,
        "max": 60_700,
    }
    assert found["scene_ms"]["total"] == 600
    assert found["human_reset_ms"]["total"] == 90_000
    assert found["verify_ms"]["total"] == 800
    assert found["reset_policy_ms"]["total"] == 0
    assert sum(found[p]["total"] for p in M.PARTS) == found["turnaround_ms"]["total"]
    assert found["with_person"] == {
        "n": 2,
        "of": 2,
        "rate": 1.0,
        "wilson95": [0.342, 1.0],
    }
    assert (found["no_next_episode"], found["open"], found["unmeasured"]) == (1, 0, 0)


def test_time_and_person_minutes_per_valid_episode():
    j = human_run()
    records = M.episodes(j.events)
    found = M.time_per_valid_episode(j.events, records)
    # The header at 0, the last line at 95300; 3 episodes sealed complete.
    assert found == {
        "span_ms": 95_300,
        "clock_domains": 1,
        "valid_episodes": 3,
        "value": 31_766.7,
    }
    person = M.automation(j.events, "human_assisted")["person_ms"]
    # 90 000 ms = 1.5 min over 3 episodes.
    assert M.human_minutes(person, records)["value"] == 0.5


def test_an_episode_not_sealed_complete_is_not_valid():
    j = Journal()
    j.move(0, "PREFLIGHT", "VERIFY_INITIAL", "preflight_passed")
    j.forward(0, 1, sealed="incomplete")
    j.move(1300, "SCENE_ASSESS", "COMPLETED", "run_completed")
    found = M.report(j.events, reset_mode="human_assisted")
    assert found["time_per_valid_episode_ms"]["valid_episodes"] == 0
    assert found["time_per_valid_episode_ms"]["value"] is None
    assert found["human_minutes_per_valid_episode"]["value"] is None


def test_empty_one_episode_and_a_run_left_waiting_never_divide_by_zero():
    empty = M.report([])
    assert empty["turnaround"]["n"] == 0
    assert empty["turnaround"]["turnaround_ms"]["mean"] is None
    assert empty["turnaround"]["with_person"]["rate"] is None
    assert empty["time_per_valid_episode_ms"] == {
        "span_ms": 0,
        "clock_domains": 0,
        "valid_episodes": 0,
        "value": None,
    }
    assert empty["human_minutes_per_valid_episode"]["value"] is None
    assert (
        empty["reset_mode"] is None and empty["mode_source"]["reset_mode"] == "unknown"
    )
    json.dumps(empty)
    # One episode, then a scene that needs a person, never resumed.
    j = Journal()
    j.move(0, "PREFLIGHT", "VERIFY_INITIAL", "preflight_passed")
    j.forward(0, 1)
    j.move(1500, "SCENE_ASSESS", "WAIT_HUMAN", "scene_reset_required")
    found = M.report(j.events, reset_mode="human_assisted")
    assert found["turnaround"]["n"] == 0 and found["turnaround"]["open"] == 1
    assert found["automation"]["person_ms"]["open_waits"] == 1
    assert found["automation"]["person_ms"]["total"] == 0
    assert found["automation"]["planned"] == 1
    assert found["human_minutes_per_valid_episode"] == {
        "person_ms": 0,
        "valid_episodes": 1,
        "value": 0.0,
        "open_waits": 1,
        "unmeasured": 0,
    }
    json.dumps(found)


def test_a_restart_on_another_clock_is_unmeasured_not_guessed():
    j = Journal()
    j.move(0, "PREFLIGHT", "VERIFY_INITIAL", "preflight_passed")
    j.forward(0, 1)  # home reached at 1200
    j.move(1500, "SCENE_ASSESS", "WAIT_HUMAN", "scene_reset_required")
    other = "host-mono:after-reboot"
    j.move(10, "WAIT_HUMAN", "FAULT_LOCKED", "recovery_ambiguous", domain=other)
    j.move(5000, "FAULT_LOCKED", "PREFLIGHT", "human_resumed", domain=other)
    j.move(5100, "PREFLIGHT", "VERIFY_INITIAL", "preflight_passed", domain=other)
    j.move(
        5400,
        "VERIFY_INITIAL",
        "FORWARD_ACTIVE",
        "scene_ready",
        episode=ep(2),
        domain=other,
    )
    found = M.turnaround(j.events)
    assert found["n"] == 0 and found["unmeasured"] == 1
    person = M.automation(j.events, "human_assisted")["person_ms"]
    assert person["unmeasured"] == 1 and person["total"] == 0
    span = M.time_per_valid_episode(j.events, M.episodes(j.events))
    # 0..1500 on the first clock, 10..5400 on the second.
    assert span["span_ms"] == 1500 + 5390 and span["clock_domains"] == 2


def test_a_stop_while_waiting_closes_the_wait():
    j = Journal()
    j.move(0, "PREFLIGHT", "VERIFY_INITIAL", "preflight_passed")
    j.move(300, "VERIFY_INITIAL", "WAIT_HUMAN", "scene_unknown")
    j.move(2300, "WAIT_HUMAN", "COMPLETED", "operator_stop")
    found = M.automation(j.events, "human_assisted")
    assert found["planned"] == 1 and found["person_ms"]["planned"] == 2000
    assert found["person_ms"]["open_waits"] == 0
    # The existing figure counts resumed waits only (its meaning is kept).
    assert found["human_wait_ms"] == 0


# --- where the mode comes from ---------------------------------------------------------------------


def test_the_mode_comes_from_the_caller_the_header_the_manifest_or_the_journal():
    j = human_run()
    assert M.mode_of(j.events, reset_mode="human_assisted")["source"] == {
        "reset_mode": "argument",
        "scene_check": "default",
    }
    # A minor-1 run header (written by the orchestrator, T-CL-06).
    j.events[0].header = SimpleNamespace(
        reset_mode="human_assisted", scene_check="operator_attested"
    )
    found = M.mode_of(j.events)
    assert (found["reset_mode"], found["scene_check"]) == (
        "human_assisted",
        "operator_attested",
    )
    assert found["source"] == {"reset_mode": "run_header", "scene_check": "run_header"}
    # A minor-0 header has no such fields: the manifest, then the journal.
    j.events[0].header = SimpleNamespace(plan_sha256="ab" * 32)
    found = M.mode_of(j.events, manifest={"after_human_resets": [{"wait_seq": 5}]})
    assert found["reset_mode"] == "human_assisted"
    assert found["source"]["reset_mode"] == "manifest"
    assert M.mode_of(j.events)["reset_mode"] is None
    with_reset = Journal()
    with_reset.move(0, "PREFLIGHT", "VERIFY_INITIAL", "preflight_passed")
    with_reset.move(300, "VERIFY_INITIAL", "RESET_ACTIVE", "scene_reset_required")
    found = M.mode_of(with_reset.events)
    assert found["reset_mode"] == "single_reset_policy"
    assert found["source"]["reset_mode"] == "journal"
    # The caller wins over what the run says; nonsense is refused.
    assert M.mode_of(with_reset.events, reset_mode="human_assisted")["reset_mode"] == (
        "human_assisted"
    )
    with pytest.raises(ValueError):
        M.mode_of([], reset_mode="single_policy")  # an alias, not a code name
    with pytest.raises(ValueError):
        M.mode_of([], scene_check="human")


def test_a_persons_scene_decisions_are_kept_out_of_the_skip_accuracy():
    decisions = [(ep(1), True), (ep(2), True), (ep(3, "reset"), False)]
    truth = {ep(1): "ready", ep(2): "reset_required", ep(3, "reset"): "reset_required"}
    machine = M.reset([], decisions, truth)
    assert machine["skip_accuracy"]["of"] == 3
    person = M.reset([], decisions, truth, by_human=True)
    assert person["skip_accuracy"] == {"n": 0, "of": 0, "rate": None, "wilson95": None}
    assert person["decisions"] == 3  # the count keeps its meaning
    assert M.scene_by_human(decisions, "operator_attested")["decisions"] == 3
    assert M.scene_by_human(decisions, "operator_attested")["skipped"] == 2
    assert M.scene_by_human(decisions, "provider")["decisions"] == 0


def test_the_report_head_names_the_mode_and_the_comparable_fields():
    found = M.report(human_run().events, reset_mode="human_assisted")
    assert found["reset_mode"] == "human_assisted"
    assert found["scene_check"] == "provider"
    assert "turnaround.turnaround_ms" in found["comparable"]
    assert "automation.unplanned" in found["comparable"]
    assert "reset" in found["mode_specific"]
    assert not set(found["comparable"]) & set(found["mode_specific"])
    # Every listed field exists in the report.
    for path in found["comparable"] + found["mode_specific"]:
        value = found
        for part in path.split("."):
            value = value[part]
    # The keys that existed before T-CL-05 keep their values, whatever the mode.
    before = (
        "interventions",
        "by_reason",
        "resumes",
        "longest_run_without_intervention",
        "current_run_without_intervention",
        "human_wait_ms",
        "human_waits_unmeasured",
    )
    unknown = M.report(human_run().events)
    assert {k: found["automation"][k] for k in before} == {
        k: unknown["automation"][k] for k in before
    }
    assert found["automation"]["interventions"] == 2
    assert found["automation"]["human_wait_ms"] == 90_000


# --- fake-driven runs of both modes ----------------------------------------------------------------


def two_episodes(tmp_path, reset_mode):
    """Episode 1, then a scene that is not ready, then episode 2: the reset
    policy puts it back (single_reset_policy) or a person does in 7 s
    (human_assisted). Fake timings: a scene check 300 ms, a reset episode
    1580 ms from its start to its end."""
    clock = fake.FakeClock()
    scene = ev.FakeSceneAssessor(
        [{"decision": "ready", "evidence": 2}, {"decision": "reset_required"}],
        clock,
        RUN,
        default={"decision": "ready", "evidence": 2},
    )
    events = ev.FakeEventStream(
        {
            "forward": [{"step": 8, "event_type": "object_settled"}],
            "reset": [{"step": 6, "event_type": "object_settled"}],
        },
        clock,
        RUN,
    )
    cfg = config(
        episodes=2,
        reset_strategy=reset_mode,
        forward_folder=FOLDERS["forward"],
        reset_folder=FOLDERS["reset"],
    )
    r = build_run(tmp_path, clock=clock, cfg=cfg, scene=scene, events=events)
    state = r.orch.run()
    if reset_mode == "human_assisted":
        assert state == "WAIT_HUMAN"
        clock.advance(7_000 * MS)
        assert r.orch.resume(
            "c-1",
            expected_seq=r.orch.journal.next_seq,
            environment_handled=True,
            health_rechecked=True,
        ).ok
        state = r.orch.run()
    assert state == "COMPLETED"
    manifest = json.loads((r.directory / MANIFEST).read_text())
    return r, M.report(
        r.orch.journal.events,
        manifest=manifest,
        termination=cfg.termination,
        max_steps=cfg.forward_max_steps,
        reset_mode=reset_mode,
    )


@pytest.mark.parametrize("reset_mode", M.RESET_MODES)
def test_both_modes_against_hand_computed_values(tmp_path, reset_mode):
    r, found = two_episodes(tmp_path, reset_mode)
    human = reset_mode == "human_assisted"
    assert found["reset_mode"] == reset_mode
    assert found["mode_source"]["reset_mode"] == "argument"
    t = found["turnaround"]
    # Home 1 reached -> forward 2 started: scene 300; then a reset of 1580
    # or a person's 7000; then a fresh initial check of 300.
    assert t["n"] == 1 and t["no_next_episode"] == 1 and t["open"] == 0
    assert t["scene_ms"]["total"] == 300 and t["verify_ms"]["total"] == 300
    assert t["reset_policy_ms"]["total"] == (0 if human else 1580)
    assert t["human_reset_ms"]["total"] == (7000 if human else 0)
    assert t["turnaround_ms"]["total"] == (7600 if human else 2180)
    assert t["with_person"]["n"] == (1 if human else 0) and t["with_person"]["of"] == 1
    au = found["automation"]
    assert (au["planned"], au["unplanned"]) == ((1, 0) if human else (0, 0))
    # Comparable across modes: no unplanned intervention in either run.
    assert au["longest_run_without_unplanned"] == 2
    assert au["longest_run_without_any_human"] == (1 if human else 2)
    hm = found["human_minutes_per_valid_episode"]
    # 7000 ms over 2 valid episodes = 0.0583 min.
    assert hm["valid_episodes"] == 2 and hm["value"] == (0.0583 if human else 0.0)
    tv = found["time_per_valid_episode_ms"]
    events = r.orch.journal.events
    span = (events[-1].mono_ns - events[0].mono_ns) // MS
    assert tv["span_ms"] == span and tv["value"] == round(span / 2, 1)
    assert found["reset"]["resets"] == (0 if human else 1)
    assert found["scene_decisions_by_human"]["decisions"] == 0
    assert found["autonomous"]["forward_episodes"] == 2
    json.dumps(found)


@pytest.mark.parametrize("reset_mode", M.RESET_MODES)
def test_the_mode_of_a_run_without_a_header_field_is_inferred(tmp_path, reset_mode):
    r, _ = two_episodes(tmp_path, reset_mode)
    found = M.report(r.orch.journal.events)  # no mode passed (old callers)
    if reset_mode == "single_reset_policy":
        assert found["reset_mode"] == reset_mode
        assert found["mode_source"]["reset_mode"] == "journal"
        assert found["automation"]["unplanned"] == 0
    else:
        # No reset episode, no manifest field: unknown, and the person's
        # reset is counted as unplanned (never flatters).
        assert found["reset_mode"] is None
        assert found["automation"]["unplanned"] == 1
        assert found["automation"]["planned"] == 0
