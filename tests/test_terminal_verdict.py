"""The terminal-aware episode verdict (``generic-release.v2``): the review's
rule over the gripper's closes, the live service's rule over the last place
time segment, and a replay of an offline evaluation that chose the rule.

Nothing here calls a model. The replay fixture holds, per episode, the review
events, the frames where the gripper closed, the outcome of the last place
time segment and what the offline evaluation (one task, two policies, 100
episodes) computed; the rules must reproduce it."""

import hashlib
import json
from pathlib import Path

import pytest

from levi.agent import anchored
from levi.agent.anchored import AnchoredSpec, outcome
from levi.live import generic, judge

FIXTURE = Path(__file__).parent / "fixtures" / "anchored" / "judge-v2-replay.json"
V1_SHA256 = "b111653582dca15a9c8f652810af8c94ef336eb95ebb5f07d77f9437cd0abc30"
# Version 2 has been used by the live service: never edited either.
V2_SHA256 = "53faf19a81727ff6ebfc3baa862eb716aaeae90386378878f78df838f6dd0349"


def release(name, task="put the object in the target"):
    return AnchoredSpec.model_validate(generic.anchored_spec(task, name))


V1 = release("generic-release.v1.json")
V2 = release("generic-release.v2.json")
V3 = release("generic-release.v3.json")


def event(frame, valid=True, verdict=None):
    verdict = verdict or ("supported" if valid else "contradicted")
    return {"frame_index": frame, "valid": valid, "verdict": verdict, "answer": {}}


def change(proposals, decisions=None):
    """A committed change set as the worker reads it."""
    return {"proposals": proposals, "decisions": decisions or {}}


def seg(subtask, start, result, episode=0):
    return {
        "kind": "segment",
        "episode_index": episode,
        "style": "subtask",
        "subtask_id": subtask,
        "start": start,
        "outcome": result,
    }


def verdict(events, closes, place, spec=V2):
    """The live verdict: the review's outcome, then the place condition."""
    result, basis = outcome(spec, events, closes=closes)
    result, basis, extra = judge.merge(result, basis, place)
    return result, basis, anchored.undecided(result, basis) or extra


# --- the replay ---------------------------------------------------------------


def episodes():
    return json.loads(FIXTURE.read_text())["episodes"]


def events_of(row):
    return [event(f, valid, v) for f, v, valid in row["events"]]


def proposals_of(row):
    """Time segments that give the episode's last place outcome (with decoys:
    an earlier place with the opposite outcome, a later segment that is not a
    place)."""
    if not row["places"]:
        return [seg("approach", 0.0, "success"), seg("retreat", 9.0, "failure")]
    last = row["last_place"]
    opposite = "failure" if last == "success" else "success"
    early = [seg("place", 1.0 + i, opposite) for i in range(row["places"] - 1)]
    return [*early, seg("place", 5.0, last), seg("retreat", 9.0, opposite)]


def test_the_fixture_is_the_whole_evaluation_and_holds_no_path():
    rows = episodes()
    assert len(rows) == 100
    text = FIXTURE.read_text()
    assert "/home" not in text and "token" not in text.lower()


def test_the_default_rule_reproduces_the_live_verdict_of_every_episode():
    for row in episodes():
        result, basis = outcome(V1, events_of(row))
        assert result == row["live"] == row["offline"]["R0"], row["id"]
        # Closes are not read by the default rule, and change nothing in it.
        assert outcome(V1, events_of(row), closes=row["closes"]) == (result, basis)
        assert set(basis) == {"valid_events", "min_valid"}


def test_the_rule_without_the_place_condition_matches_the_offline_rule_2():
    for row in episodes():
        result, basis = outcome(V2, events_of(row), closes=row["closes"])
        assert result == row["offline"]["R2"], row["id"]
        assert basis["rule"] == "last_valid_not_regrasped"


def test_version_3_matches_offline_rule_2_episode_by_episode():
    """Version 3 is the whole live verdict without time segments: the review's
    outcome over the closes, no place condition (the worker merges nothing)."""
    for row in episodes():
        result, basis = outcome(V3, events_of(row), closes=row["closes"])
        assert result == row["offline"]["R2"], row["id"]
        assert basis["rule"] == "last_valid_not_regrasped"
        assert basis["require_place"] is False and "missing_inputs" not in basis
        assert anchored.undecided(result, basis) is False
    # Without the closes a success cannot be decided; a failure stays one.
    held = [event(18)]
    result, basis = outcome(V3, held, closes=None)
    assert result == "success" and basis["missing_inputs"] == ["closes"]
    assert anchored.undecided(result, basis) is True


def test_the_full_rule_matches_the_offline_rule_5_episode_by_episode():
    for row in episodes():
        place = judge.place_state(change(proposals_of(row)), 0)
        result, basis, undecided = verdict(events_of(row), row["closes"], place)
        offline = row["offline"]["R5"]
        got = "undecided" if undecided else result
        assert got == offline, (row["id"], got, offline, basis)
        if undecided:
            # An undecided verdict is recorded as a failure.
            assert result == "failure"


def test_the_full_rule_has_no_false_success_and_misses_no_success():
    tp = fn = fp = tn = 0
    for row in episodes():
        if row["truth"] not in ("success", "failure"):
            continue
        place = judge.place_state(change(proposals_of(row)), 0)
        result, _, _ = verdict(events_of(row), row["closes"], place)
        if row["truth"] == "success":
            tp += result == "success"
            fn += result != "success"
        else:
            fp += result == "success"
            tn += result != "success"
    assert (tp, fn, fp, tn) == (30, 0, 0, 61)
    # The default rule on the same episodes: the false successes this removes.
    wrong = sum(
        row["truth"] == "failure" and row["live"] == "success" for row in episodes()
    )
    assert wrong == 12


# --- the review's rule over the closes ---------------------------------------


def test_picked_up_again_after_the_last_valid_release_is_a_failure():
    result, basis = outcome(V2, [event(100)], closes=[40, 150])
    assert result == "failure"
    assert basis["rule"] == "last_valid_not_regrasped"
    assert basis["last_valid_frame"] == 100
    assert basis["closes_after_last_valid"] == 1
    assert basis["valid_events"] == 1 and basis["min_valid"] == 1
    # Closing before the release is how it was held.
    assert outcome(V2, [event(100)], closes=[40])[0] == "success"
    # A release and a close in the same frame is not "after".
    assert outcome(V2, [event(100)], closes=[100])[0] == "success"


def test_the_last_release_not_being_valid_does_not_hide_a_regrasp():
    # Valid at 50, regrasped (close at 120), released again at 150 and judged
    # not valid: the object was picked up after its last valid release.
    events = [event(50), event(150, valid=False)]
    result, basis = outcome(V2, events, closes=[20, 120])
    assert result == "failure" and basis["closes_after_last_valid"] == 1
    assert basis["last_valid_frame"] == 50
    # Without the close in between it holds, as the rule reads valid releases.
    assert outcome(V2, events, closes=[20])[0] == "success"


def test_no_valid_release_is_a_failure_whatever_the_closes():
    result, basis = outcome(V2, [event(50, valid=False)], closes=[])
    assert result == "failure"
    assert basis["last_valid_frame"] is None
    assert basis["closes_after_last_valid"] is None
    assert outcome(V2, [], closes=[])[0] == "failure"


def test_min_valid_stays_a_necessary_condition():
    spec = AnchoredSpec.model_validate(
        {
            **json.loads(generic.text("generic-release.v2.json")),
            "episode": {"min_valid": 2, "rule": "last_valid_not_regrasped"},
        }
        | {"question": "Did it land? {task}"}
    )
    assert outcome(spec, [event(10)], closes=[])[0] == "failure"
    assert outcome(spec, [event(10), event(20)], closes=[])[0] == "success"
    assert outcome(spec, [event(10), event(20)], closes=[30])[0] == "failure"


def test_a_record_without_closes_falls_back_and_says_so():
    result, basis = outcome(V2, [event(100)], closes=None)
    assert result == "success"
    assert basis["missing_inputs"] == ["closes", "place"]
    assert basis["closes_after_last_valid"] is None
    # Not silently a success: it is undecided for a person.
    assert anchored.undecided(result, basis)
    # A failure it would have been anyway needs no doubt.
    result, basis = outcome(V2, [event(100, valid=False)], closes=None)
    assert result == "failure" and not anchored.undecided(result, basis)


def test_the_default_rule_is_unchanged_and_its_frozen_form_is_too():
    assert V1.episode.rule == "any_valid" and V1.episode.require_place is False
    frozen = anchored.dump(V1)
    assert frozen["episode"] == {
        "label_field": None,
        "require_labels": [],
        "min_valid": 1,
    }
    assert anchored.dump(V2)["episode"] == {
        "label_field": None,
        "require_labels": [],
        "min_valid": 1,
        "rule": "last_valid_not_regrasped",
        "require_place": True,
    }
    # The built-in specs freeze exactly what they froze before.
    for spec in anchored.builtin().values():
        assert "rule" not in anchored.dump(spec)["episode"]
        assert "require_place" not in anchored.dump(spec)["episode"]


def test_a_rule_is_refused_where_it_cannot_apply():
    base = json.loads(generic.text("generic-release.v2.json"))
    base["question"] = "Did it land? {task}"

    def spec(**episode):
        return AnchoredSpec.model_validate({**base, "episode": episode})

    with pytest.raises(ValueError, match="goes with episode.rule"):
        spec(require_place=True)
    with pytest.raises(ValueError, match="without require_labels"):
        spec(
            rule="last_valid_not_regrasped",
            label_field="landed",
            require_labels=["at_target"],
        )
    with pytest.raises(ValueError, match="anchor event must be open"):
        AnchoredSpec.model_validate(
            {**base, "anchor": {"signal": "gripper", "event": "close"}}
        )
    with pytest.raises(ValueError):
        spec(rule="whatever")


def test_the_spec_files():
    one, two = (json.loads(generic.text(f"generic-release.v{n}.json")) for n in (1, 2))
    assert one["version"] == 1 and two["version"] == 2
    assert two["status"] == "candidate" and two["id"] == one["id"]
    assert two["episode"] == {
        "min_valid": 1,
        "rule": "last_valid_not_regrasped",
        "require_place": True,
    }
    # Only the version, the words about the rule, and the episode rule differ.
    same = ("anchor", "views", "question", "fields", "valid_when", "unknown_values")
    assert all(one[k] == two[k] for k in same)
    assert one["max_output_tokens"] == two["max_output_tokens"]
    assert set(one) == set(two)
    # The used v1 file is never edited.
    assert (
        hashlib.sha256(generic.path("generic-release.v1.json").read_bytes()).hexdigest()
        == V1_SHA256
    )
    assert "rule" not in one["episode"]
    assert generic.manifest(_config("generic-release.v2.json"))[
        "generic-release.v2.json"
    ]


def test_version_3_is_version_2_without_the_place_condition():
    two, three = (
        json.loads(generic.text(f"generic-release.v{n}.json")) for n in (2, 3)
    )
    same = (
        "anchor",
        "views",
        "question",
        "fields",
        "valid_when",
        "unknown_values",
        "max_output_tokens",
    )
    assert all(two[k] == three[k] for k in same)
    assert set(two) == set(three)
    assert three["id"] == two["id"] == "generic-release"
    assert three["version"] == 3 and three["status"] == "candidate"
    assert three["episode"] == {
        "min_valid": 1,
        "rule": "last_valid_not_regrasped",
        "require_place": False,
    }
    assert three["title"] == {
        "en": "Generic release review, gripper rule without time segments (candidate v3)",
        "zh": "通用释放复核，夹爪规则、不需要时间片段（候选 v3）",
    }
    assert "pipeline.temporal = false" in three["description"]
    assert "CANDIDATE, NOT THE DEFAULT" in three["description"]
    # Its frozen form: the place condition is simply absent.
    assert anchored.dump(V3)["episode"] == {
        "label_field": None,
        "require_labels": [],
        "min_valid": 1,
        "rule": "last_valid_not_regrasped",
    }
    assert (
        hashlib.sha256(generic.path("generic-release.v2.json").read_bytes()).hexdigest()
        == V2_SHA256
    )


def _config(spec):
    from levi.live import config as live_config

    c = live_config.Config()
    c.pipeline.anchored_spec = spec
    return c


def test_the_default_configuration_still_names_version_1():
    from levi.live import config as live_config

    assert live_config.Config().pipeline.anchored_spec == "generic-release.v1.json"


# --- the live service's rule over the place time segments --------------------


def test_the_last_place_is_the_one_that_starts_last():
    proposals = [
        seg("place", 8.0, "success"),
        seg("place", 2.0, "failure"),
        seg("retreat", 9.0, "failure"),
        seg("place", 3.0, "failure", episode=1),
    ]
    got = change(proposals)
    assert judge.place_state(got, 0) == "success"
    assert judge.place_state(got, 1) == "failure"
    assert judge.place_state(got, 2) == "none"
    assert judge.place_state(change([]), 0) == "none"
    # Of two that start together, the one listed last.
    both = change([seg("place", 1.0, "success"), seg("place", 1.0, "failure")])
    assert judge.place_state(both, 0) == "failure"
    # A segment with no outcome is unknown, never a success.
    assert judge.place_state(change([seg("place", 1.0, None)]), 0) == "unknown"
    assert judge.place_state(change([seg("place", 1.0, "unknown")]), 0) == "unknown"
    assert judge.place_state(None, 0) == "missing"


def test_a_segment_a_person_rejected_does_not_count():
    proposals = [
        seg("place", 2.0, "success"),
        seg("place", 8.0, "failure"),  # index 1, rejected in review
        seg("retreat", 9.0, "success"),
    ]
    assert judge.place_state(change(proposals), 0) == "failure"
    assert judge.place_state(change(proposals, {"1": "rejected"}), 0) == "success"
    # Accepted or undecided ones stay; rejecting the only place leaves none.
    assert judge.place_state(change(proposals, {"1": "accepted"}), 0) == "failure"
    only = change([seg("place", 2.0, "success")], {"0": "rejected"})
    assert judge.place_state(only, 0) == "none"


def test_only_segments_count_as_place_segments():
    other = {**seg("place", 9.0, "failure"), "kind": "outcome"}
    got = change([seg("place", 2.0, "success"), other])
    assert judge.place_state(got, 0) == "success"


def test_the_place_outcome_decides_a_release_that_held():
    events, closes = [event(100)], [40]
    assert verdict(events, closes, "success")[0::2] == ("success", False)
    for place in ("failure", "none"):
        result, basis, undecided = verdict(events, closes, place)
        assert (result, undecided) == ("failure", False)
        assert basis["place_outcome"] == place
    result, basis, undecided = verdict(events, closes, "unknown")
    assert (result, undecided) == ("failure", True)
    # Once the place is read, the review's "place is missing" is gone.
    result, basis, undecided = verdict(events, closes, "success")
    assert "missing_inputs" not in basis and basis["place_outcome"] == "success"


def test_a_failure_already_stays_a_failure_and_is_not_undecided():
    # Regrasped, place unknown: failed on the gripper alone.
    result, basis, undecided = verdict([event(100)], [150], "unknown")
    assert (result, undecided) == ("failure", False)
    assert basis["place_outcome"] == "unknown"
    # No valid release.
    assert verdict([], [], "success")[0] == "failure"


def test_without_time_segments_it_is_a_failure_that_is_undecided_and_says_so():
    result, basis, undecided = verdict([event(100)], [40], "missing")
    assert (result, undecided) == ("failure", True)  # same as an unknown place
    assert basis["missing_inputs"] == ["place"]
    assert basis["place_outcome"] == "missing"
    assert verdict([event(100)], [40], "unknown")[0::2] == (result, undecided)
    # Both inputs missing are both named.
    result, basis = outcome(V2, [event(100)], closes=None)
    assert basis["missing_inputs"] == ["closes", "place"]
    result, basis, _ = judge.merge(result, basis, "missing")
    assert basis["missing_inputs"] == ["closes", "place"]
    # With the place read, only the closes stay missing, and a success is undecided.
    result, basis, undecided = judge.merge(
        *outcome(V2, [event(100)], closes=None), "success"
    )
    assert basis["missing_inputs"] == ["closes"]
    assert result == "success" and undecided is False
    assert anchored.undecided(result, basis)
    # Failing on what is known stays a failure and is not undecided.
    assert verdict([event(100)], [150], "missing")[0::2] == ("failure", False)


def test_the_review_record_alone_never_holds_a_certain_success():
    """The review cannot see the time segments, so under require_place its own
    success names the missing place: undecided for a training manifest or a
    person accepting the proposal, until the live verdict applies it."""
    result, basis = outcome(V2, [event(100)], closes=[40])
    assert result == "success" and basis["missing_inputs"] == ["place"]
    assert anchored.undecided(result, basis)
    # A failure needs no doubt; the default rule and the closes-only rule have none.
    result, basis = outcome(V2, [event(100)], closes=[150])
    assert result == "failure" and not anchored.undecided(result, basis)
    result, basis = outcome(V1, [event(100)])
    assert "missing_inputs" not in basis and not anchored.undecided(result, basis)
    only = AnchoredSpec.model_validate(
        {
            **json.loads(generic.text("generic-release.v2.json")),
            "question": "Did it land? {task}",
            "episode": {"rule": "last_valid_not_regrasped"},
        }
    )
    result, basis = outcome(only, [event(100)], closes=[40])
    assert result == "success" and not anchored.undecided(result, basis)


def test_a_review_without_the_place_rule_passes_through_untouched():
    result, basis = outcome(V1, [event(100)])
    assert judge.merge(result, basis, "failure") == (result, basis, False)
    # The rule over the closes alone has no place condition either.
    spec = AnchoredSpec.model_validate(
        {
            **json.loads(generic.text("generic-release.v2.json")),
            "question": "Did it land? {task}",
            "episode": {"rule": "last_valid_not_regrasped"},
        }
    )
    result, basis = outcome(spec, [event(100)], closes=[])
    assert basis["require_place"] is False
    assert judge.merge(result, basis, "failure") == (result, basis, False)


def test_a_verdict_without_the_time_segments_is_not_counted_as_a_success():
    from levi.live import stats

    def row(demo, outcome, undecided, **extra):
        return stats.normalize(
            {
                "dataset": "g__t",
                "demo": demo,
                "session": "s",
                "result": {
                    "state": "done",
                    "verdict": {"outcome": outcome, "undecided": undecided, **extra},
                },
            }
        )

    _, basis, undecided = verdict([event(100)], [40], "missing")
    result = verdict([event(100)], [40], "missing")[0]
    rows = [
        row("demo_0000", result, undecided, place_outcome=basis["place_outcome"]),
        row("demo_0001", "success", False, place_outcome="success"),
    ]
    summary = stats.summarize(rows)["outcome"]
    assert summary["verdicts"] == {"failure": 1, "success": 1}
    assert summary["undecided"] == 1
