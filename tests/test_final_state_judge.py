"""The final-state judgement (``generic-final.v1``): one question per episode on
the last seconds of the recording, the rule ``final_state``, and the live
settings that run it. Nothing here calls a real model; the end-to-end parts use
the protocol fakes of the other anchored-review and live-service tests."""

import json
import re

import pandas as pd
import pytest
from test_openai_local import (  # noqa: F401
    camera_dataset,
    completion,
    config,
    guard,
    server,
)

from levi.agent import anchored
from levi.agent.anchored import AnchoredSpec, outcome, view_rows
from levi.agent.planning import approve
from levi.agent.schema import Budget, TaskContext
from levi.live import config as live_config
from levi.live import generic

NAME = "generic-final.v1.json"
TASK = "put the object on the target"


def final():
    return AnchoredSpec.model_validate(generic.anchored_spec(TASK, NAME))


def event(verdict, frame=29, **answer):
    return {
        "frame_index": frame,
        "valid": verdict == "supported",
        "verdict": verdict,
        "answer": answer,
    }


def answered(object_state, stable):
    """The event an answer makes, through the spec's own conditions."""
    spec = final()
    answer = {"object_state": object_state, "stable": stable}
    checks, verdict = anchored.judge(spec, answer)
    return {
        "frame_index": 29,
        "valid": verdict == "supported",
        "verdict": verdict,
        "answer": answer,
        "checks": checks,
    }


# --- the spec -----------------------------------------------------------------


def test_the_spec_file_is_a_general_candidate_with_both_languages():
    raw = json.loads(generic.text(NAME))
    spec = final()
    assert (raw["id"], raw["version"], raw["status"]) == (
        "generic-final",
        1,
        "candidate",
    )
    assert set(raw["title"]) == {"en", "zh"} and "{task}" in raw["question"]
    assert spec.anchor.event == "end" and spec.episode.rule == "final_state"
    assert [f.name for f in spec.fields] == ["object_state", "stable"]
    assert {v.camera for v in spec.views} == {
        "observation.images.view1",
        "observation.images.hand",
    }
    # Every offset counts back from the last frame; none is after it.
    assert all(max(v.offsets_seconds) <= 0 for v in spec.views)
    assert all(v.at == "anchor" for v in spec.views)
    # Nothing in it is about a task: no object or place names.
    words = re.findall(r"[a-z]+", json.dumps(raw).lower())
    assert not {"plate", "plates", "eggplant", "screw", "screws", "box", "bowl"} & set(
        words
    )
    assert "NOT EVALUATED" in raw["description"]
    assert len(raw["description"]) <= 2000
    assert anchored.title_of(spec, "zh") and anchored.title_of(spec, "en")


def test_its_frozen_form_keeps_the_end_anchor_and_the_rule():
    frozen = anchored.dump(final())
    assert frozen["anchor"]["event"] == "end"
    assert frozen["episode"] == {
        "label_field": None,
        "require_labels": [],
        "min_valid": 1,
        "rule": "final_state",
    }
    assert frozen["status"] == "candidate" and "start" not in frozen
    assert anchored.cameras(frozen) == [
        "observation.images.view1",
        "observation.images.hand",
    ]
    # A plan names no built-in spec of this kind: the live service gives it whole.
    assert "generic-final" not in anchored.builtin()
    assert anchored.resolve(frozen) == frozen


def test_the_spec_is_refused_where_it_cannot_apply():
    base = json.loads(generic.text(NAME))
    base["question"] = "Where is it? {task}"

    def spec(anchor=None, **episode):
        return AnchoredSpec.model_validate(
            {
                **base,
                **({"anchor": anchor} if anchor else {}),
                "episode": {**base["episode"], **episode},
            }
        )

    assert spec().anchor.event == "end"
    # The end anchor goes with the rule, and only with it.
    with pytest.raises(ValueError, match="goes with anchor.event end"):
        spec(anchor={"signal": "gripper", "event": "open"})
    with pytest.raises(ValueError, match="goes with anchor.event end"):
        spec(rule="any_valid")
    with pytest.raises(ValueError, match="anchor event must be open"):
        spec(rule="last_valid_not_regrasped")
    # One event per episode: no second valid event to ask for, no place segment.
    with pytest.raises(ValueError, match="neither require_place nor a min_valid"):
        spec(min_valid=2)
    with pytest.raises(ValueError, match="neither require_place nor a min_valid"):
        spec(require_place=True)
    with pytest.raises(ValueError, match="without require_labels"):
        spec(label_field="object_state", require_labels=["elsewhere"])


# --- the rule -----------------------------------------------------------------


@pytest.mark.parametrize(
    "object_state,stable,result,is_undecided",
    [
        ("resting_at_destination", "yes", "success", False),
        # Not at rest on the destination: failures, whatever stable says.
        ("held_over_destination", "yes", "failure", False),
        ("elsewhere", "yes", "failure", False),
        ("in_gripper", "yes", "failure", False),
        ("resting_at_destination", "no", "failure", False),
        ("elsewhere", "no", "failure", False),
        # Anything unread that no definite answer outweighs: undecided.
        ("unclear", "yes", "failure", True),
        ("resting_at_destination", "unclear", "failure", True),
        ("unclear", "unclear", "failure", True),
        # A definite failure outweighs an unclear answer next to it.
        ("elsewhere", "unclear", "failure", False),
        ("in_gripper", "unclear", "failure", False),
        ("unclear", "no", "failure", False),
    ],
)
def test_the_final_state_rule(object_state, stable, result, is_undecided):
    spec = final()
    got, basis = outcome(spec, [answered(object_state, stable)])
    assert got == result
    assert anchored.undecided(got, basis) is is_undecided
    assert basis["rule"] == "final_state" and basis["min_valid"] == 1
    assert basis["valid_events"] == (1 if result == "success" else 0)
    assert basis["final_reading"] == (
        "supported"
        if result == "success"
        else "unknown"
        if is_undecided
        else "contradicted"
    )
    assert "missing_inputs" not in basis


def test_the_rule_needs_no_gripper_close_and_no_time_segment():
    spec = final()
    ok = [answered("resting_at_destination", "yes")]
    # The same whatever the gripper did, and whether or not closes were kept.
    assert outcome(spec, ok)[0] == outcome(spec, ok, closes=[3, 40])[0] == "success"
    assert outcome(spec, ok, closes=[])[1] == outcome(spec, ok)[1]
    assert "place" not in outcome(spec, ok)[1].get("missing_inputs", [])


def test_an_episode_with_no_frame_to_read_is_a_failure_that_is_undecided():
    got, basis = outcome(final(), [])
    assert got == "failure" and anchored.undecided(got, basis)
    assert basis["final_reading"] is None


def test_the_other_rules_decide_as_before():
    base = json.loads(generic.text("generic-release.v3.json"))
    base["question"] = "Did it land? {task}"
    spec = AnchoredSpec.model_validate(base)
    events = [event("supported", frame=5, landed="at_target")]
    # No gripper close after the last valid release: success; one: failure.
    assert outcome(spec, events, closes=[2]) == (
        "success",
        {
            "valid_events": 1,
            "min_valid": 1,
            "rule": "last_valid_not_regrasped",
            "last_valid_frame": 5,
            "closes_after_last_valid": 0,
            "require_place": False,
        },
    )
    got, basis = outcome(spec, events, closes=[2, 9])
    assert got == "failure" and basis["closes_after_last_valid"] == 1
    # The final-state keys do not leak into another rule's basis.
    assert "final_reading" not in basis
    assert not anchored.undecided(got, basis)
    assert anchored.undecided(*outcome(spec, events))  # no closes: undecided


def test_the_used_spec_files_are_unchanged_and_none_but_the_new_one_ends_an_episode():
    for name in ("generic-release.v1.json", "generic-release.v2.json"):
        raw = json.loads(generic.text(name))
        assert raw["anchor"]["event"] == "open"
        assert raw["episode"].get("rule") != "final_state"
    v3 = json.loads(generic.text("generic-release.v3.json"))
    assert v3["anchor"]["event"] == "open"
    assert v3["episode"]["rule"] == "last_valid_not_regrasped"
    for spec in anchored.builtin().values():
        assert spec.anchor.event in ("open", "close")
        assert "final_state" not in json.dumps(anchored.dump(spec))


# --- the frames ---------------------------------------------------------------


def test_the_anchor_is_the_last_frame_whatever_the_length_and_reads_no_gripper():
    spec = final()
    for n in (1, 2, 20, 300):
        table = pd.DataFrame({"frame_index": range(n)})  # no state column at all
        rows, closes, channel = anchored.anchor_rows(table, {}, None, spec.anchor)
        assert rows == [n - 1] and closes == [] and channel == "episode end"
    assert (
        anchored.anchor_rows(pd.DataFrame({"frame_index": []}), {}, None, spec.anchor)[
            0
        ]
        == []
    )


def test_a_short_episode_shows_its_first_frame_in_place_of_missing_ones():
    spec = final()
    side, wrist = spec.views
    # 10 fps: the side camera looks back 30, 20, 12, 6, 2 and 0 frames.
    assert view_rows(side, 10.0, 19, 19) == [0, 0, 7, 13, 17, 19]
    assert view_rows(wrist, 10.0, 19, 19) == [0, 9, 15, 19]
    # A 5-frame episode and a 1-frame one stay inside [0, last].
    assert view_rows(side, 10.0, 4, 4) == [0, 0, 0, 0, 2, 4]
    assert view_rows(side, 10.0, 0, 0) == [0] * 6
    assert view_rows(wrist, 30.0, 0, 0) == [0] * 4
    # A fast recording reaches further back in frames, never past the start.
    assert view_rows(side, 100.0, 49, 49)[0] == 0


# --- one review through the real code -----------------------------------------


def replies(fake, answers):
    def reply(payload):
        return completion(json.dumps(answers.pop(0)))

    fake.reply = reply


def one_frame(dataset, episode):
    path = dataset / f"data/chunk-000/episode_{episode:06d}.parquet"
    pd.read_parquet(path).iloc[:1].to_parquet(path)


def test_a_review_asks_once_per_episode_on_its_last_frames(
    client,
    dataset,
    server,  # noqa: F811 - the fixture imported above
):
    from levi import catalog, service
    from levi.agent.runtime import Workbench

    camera = camera_dataset(dataset)
    one_frame(dataset, 1)  # episode 0: 20 frames, episode 1: a single frame
    entry = catalog.register(str(dataset))
    wb = Workbench(service.STATE)
    wb.store.put("providers", "vllm", config().model_dump())
    spec = generic.anchored_spec(TASK, NAME)
    for view in spec["views"]:
        view["camera"] = camera
    context = TaskContext(
        repo_id=entry["id"],
        episodes=[0, 1],
        instruction="Judge each ending",
        provider="vllm",
        cameras=[camera],
        allow_media_egress=True,
        workflow={"kind": "review", "anchored": spec, "require_human_pilot": False},
        budget=Budget(max_calls=10, max_tokens=None, max_seconds=600),
    )
    run = wb.plan(context)
    estimate = run["plan"]["estimate"]
    assert "per episode on the frames at its end" in estimate["basis"]
    assert "gripper" not in estimate["basis"] and estimate["minimum_requests"] == 0
    assert run["plan"]["anchored_spec"]["status"] == "candidate"
    approve(wb, run["id"], 1, "human")
    replies(
        server,
        [
            {"object_state": "resting_at_destination", "stable": "yes"},
            {"object_state": "unclear", "stable": "yes"},
        ],
    )
    assert wb.store.claim(run["id"], "owner")
    wb.execute(run["id"], "owner", pilot=False)
    result = wb.store.get("runs", run["id"])
    assert result["status"] == "waiting_for_review", result["reason"]
    # One request per episode, with the images then the question (the task in it).
    chats = server.chats()
    assert len(chats) == 2 and result["requests"] == 2
    for chat in chats:
        (message,) = chat["messages"]
        kinds = [p["type"] for p in message["content"]]
        assert kinds == ["image_url"] * 10 + ["text"]
        assert TASK in message["content"][-1]["text"]
        assert "{task}" not in message["content"][-1]["text"]
        schema = chat["response_format"]["json_schema"]["schema"]
        assert list(schema["properties"]) == ["object_state", "stable"]
    first = wb.store.get("anchored", f"{run['id']}:0")
    assert first["event"] == "end" and first["channel"] == "episode end"
    assert "closes" not in first
    (e,) = first["events"]
    assert e["frame_index"] == 19 and e["valid"] and e["verdict"] == "supported"
    shown = {}
    for f in e["frames"]:
        shown.setdefault(f["role"], []).append(f["frame_index"])
    assert shown == {"side": [0, 0, 7, 13, 17, 19], "wrist": [0, 9, 15, 19]}
    assert first["outcome"] == "success"
    assert first["basis"]["rule"] == "final_state"
    # The one-frame episode: every image is its only frame, nothing crashes.
    second = wb.store.get("anchored", f"{run['id']}:1")
    (e,) = second["events"]
    assert e["frame_index"] == 0 and {f["frame_index"] for f in e["frames"]} == {0}
    assert second["outcome"] == "failure" and e["verdict"] == "unknown"
    assert anchored.undecided(second["outcome"], second["basis"])
    # The proposals: a success, and a failure that says it is undecided.
    change = wb.store.get("changes", result["changes"])
    by_episode = {p["episode_index"]: p for p in change["proposals"]}
    assert by_episode[0]["outcome"] == "success" and not by_episode[0]["uncertainty"]
    assert by_episode[1]["outcome"] == "failure"
    assert "could not be read" in by_episode[1]["uncertainty"]
    assert "final-state check" in by_episode[0]["content"]
    assert "gripper" not in by_episode[0]["content"].split(". Outcome")[0].lower()
    # Nothing here wrote an outcome label.
    assert not list(service.STATE.rglob("outcomes"))


# --- the live settings --------------------------------------------------------


def load(tmp_path, toml):
    path = tmp_path / "live.toml"
    path.write_text("[pipeline]\n" + toml)
    return live_config.load(path)


def test_review_only_runs_with_the_final_state_spec(tmp_path):
    c = load(tmp_path, f"temporal = false\nanchored_spec = '{NAME}'\n")
    assert c.pipeline.temporal is False and c.pipeline.anchored_spec == NAME
    # With time segments on, it is allowed too (it just does not need them).
    assert load(tmp_path, f"anchored_spec = '{NAME}'\n").pipeline.temporal is True
    assert generic.manifest(c) == {NAME: generic.sha256(NAME)}


def test_a_min_valid_above_one_is_refused_with_the_final_state_spec(tmp_path):
    with pytest.raises(ValueError, match="anchored_min_valid = 2 cannot be used with"):
        load(
            tmp_path,
            f"temporal = false\nanchored_spec = '{NAME}'\nanchored_min_valid = 2\n",
        )
    with pytest.raises(ValueError, match="cannot have more than one valid event"):
        load(tmp_path, f"anchored_spec = '{NAME}'\nanchored_min_valid = 3\n")
    # The release specs keep taking it, and 1 is fine for every spec.
    load(tmp_path, "anchored_min_valid = 2\n")
    load(
        tmp_path,
        "temporal = false\nanchored_spec = 'generic-release.v3.json'\nanchored_min_valid = 2\n",
    )
    load(tmp_path, f"anchored_spec = '{NAME}'\nanchored_min_valid = 1\n")


def test_the_place_condition_message_names_the_spec_that_can_run():
    with pytest.raises(ValueError, match="generic-final.v1.json"):
        p = live_config.Pipeline(
            temporal=False, anchored_spec="generic-release.v2.json"
        )
        live_config.Config(pipeline=p).validate()
