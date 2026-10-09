"""The final-state judgement with a start check (``generic-final.v2``): one more
question per episode on the first frames, ``start_state``. An object that was
already at its destination makes the episode no valid trial (a failure that is
undecided); an unreadable start never lets a success stand; no start answer
(a caller that cannot send the first frames) is version 1's rule. The old
spec files stay as they were. No real model: the rule over answers, and one
review through the real code against the protocol fake."""

import hashlib
import json
import re
from pathlib import Path

import pytest
from test_live_pipeline import env  # noqa: F401  (fixture)
from test_openai_local import (  # noqa: F401
    camera_dataset,
    completion,
    config,
    guard,
    server,
)

from levi.agent import anchored
from levi.agent.anchored import AnchoredSpec, outcome
from levi.agent.planning import approve
from levi.agent.schema import Budget, TaskContext
from levi.live import config as live_config
from levi.live import generic

V1 = "generic-final.v1.json"
V2 = "generic-final.v2.json"
TASK = "pick the eggplant on the bread"
CONVENTION = "'pick X in/on/into/onto Y' means pick X up and put it in/on Y"


def spec2(task=TASK, name=V2):
    return AnchoredSpec.model_validate(generic.anchored_spec(task, name))


def answered(spec, object_state, stable="yes"):
    answer = {"object_state": object_state, "stable": stable}
    checks, verdict = anchored.judge(spec, answer)
    return {
        "frame_index": 29,
        "valid": verdict == "supported",
        "verdict": verdict,
        "answer": answer,
        "checks": checks,
    }


# --- the files ------------------------------------------------------------------


def test_the_used_spec_files_are_byte_for_byte_what_they_were():
    # A file that has been used is never edited: a change is a new version.
    assert {
        n: generic.sha256(n)
        for n in (
            "generic-final.v1.json",
            "generic-release.v1.json",
            "generic-release.v2.json",
            "generic-release.v3.json",
            "generic-definitions.v1.json",
            "generic-guideline.v1.md",
            "generic-vocabulary.v1.json",
        )
    } == {
        "generic-final.v1.json": "8db7eeb53ce8c47c3f1f16004814b152259daa24c3c3973108290c88d313813d",
        "generic-release.v1.json": "b111653582dca15a9c8f652810af8c94ef336eb95ebb5f07d77f9437cd0abc30",
        "generic-release.v2.json": "53faf19a81727ff6ebfc3baa862eb716aaeae90386378878f78df838f6dd0349",
        "generic-release.v3.json": "5a46e936aa5356295cddfe885e7fb137f01381658939107a835fe0af99ab82fb",
        "generic-definitions.v1.json": "e846f31f1c19201677e0e324ace425a5dfcd3745eab9caa9559408fe9fce7f66",
        "generic-guideline.v1.md": "582af9bd27deeb5d59be9c9e212a546ef819f63febbb671672419768ec039fc9",
        "generic-vocabulary.v1.json": "a47a7d1e315e1dc14f36e9f072231acc58200fd226eb6179b8d91b05fe58e548",
    }
    # So are the shipped plates specs, and what a plan freezes of them.
    shipped = Path(anchored.__file__).parent / "anchored_specs"
    assert {
        n: hashlib.sha256((shipped / n).read_bytes()).hexdigest()
        for n in ("plates-release.json", "plates-release-3.json")
    } == {
        "plates-release.json": "fc950413a8b1629430942a19027febe7e01d8975ee57e515835f3a24749f2ee8",
        "plates-release-3.json": "8077558afc2a7f37369b034c3deeebc9bcb8fd66fb9e1ab31d97a0b499da4898",
    }
    frozen = {
        sid: hashlib.sha256(
            json.dumps(anchored.dump(anchored.lookup(sid)), sort_keys=True).encode()
        ).hexdigest()
        for sid in ("plates-release", "plates-release-3")
    }
    assert frozen == {
        "plates-release": "1f83036b8cc7dcf2947ea561e9ba4954e92ff03ce779fdf54faa5b4d16d283b1",
        "plates-release-3": "379e6f8351701fc86315e36b78a7a1d1771762eba92c820331e8617f551353a2",
    }


def test_version_2_is_the_final_state_question_plus_the_convention_and_a_start_check():
    one, two = json.loads(generic.text(V1)), json.loads(generic.text(V2))
    assert (two["id"], two["version"], two["status"]) == (
        "generic-final",
        2,
        "candidate",
    )
    assert set(two["title"]) == {"en", "zh"} and len(two["description"]) <= 2000
    assert "NOT EVALUATED" in two["description"]
    # The final frames' question, rule and answer are version 1's ...
    for key in ("anchor", "views", "fields", "valid_when", "unknown_values", "episode"):
        assert two[key] == one[key], key
    assert two["max_output_tokens"] == one["max_output_tokens"]
    # ... with the lab's reading of 'pick X on Y' added once, in both questions.
    assert CONVENTION in two["question"] and CONVENTION not in one["question"]
    assert (
        two["question"].replace(two["question"].split("\n")[1] + "\n", "")
        == one["question"]
    )
    assert CONVENTION in two["start"]["question"]
    # Nothing in it is about a task.
    words = re.findall(r"[a-z]+", json.dumps(two).lower())
    assert not {"plate", "plates", "eggplant", "bread", "screw", "bowl"} & set(words)


def test_the_start_check_asks_the_first_frame_of_both_cameras():
    spec = spec2()
    assert spec.anchor.event == "end" and spec.episode.rule == "final_state"
    start = spec.start
    assert [(v.role, v.camera, v.at, v.offsets_seconds) for v in start.views] == [
        ("start_side", "observation.images.view1", "start", [0.0]),
        ("start_wrist", "observation.images.hand", "start", [0.0]),
    ]
    assert [(f.name, f.enum) for f in start.fields] == [
        ("start_state", ["already_at_destination", "not_at_destination", "unclear"])
    ]
    assert [(c.field, c.is_in) for c in start.void_when] == [
        ("start_state", ["already_at_destination"])
    ]
    assert start.waive == []
    # The task instruction is quoted in both questions, once filled in.
    assert TASK in spec.question and TASK in start.question
    assert "{task}" not in spec.question + start.question
    # The roles a client names its images by do not clash.
    assert not {v.role for v in spec.views} & {v.role for v in start.views}
    assert anchored.cameras(spec) == [
        "observation.images.view1",
        "observation.images.hand",
    ]


def test_the_frozen_form_keeps_void_when_and_leaves_out_what_is_empty():
    frozen = anchored.dump(spec2())
    assert "void_when" in frozen["start"] and "waive" not in frozen["start"]
    assert all("at" in v for v in frozen["start"]["views"])
    assert anchored.resolve(frozen) == frozen
    # A spec that waives labels is frozen as before: waive, and no void_when.
    plates = anchored.dump(anchored.lookup("plates-release-3"))
    assert plates["start"]["waive"] and "void_when" not in plates["start"]


def test_a_start_check_must_do_one_thing_and_for_the_right_rule():
    raw = json.loads(generic.text(V2))

    def spec(**start):
        out = {**raw, "start": {**raw["start"], **start}}
        out["question"] = out["question"].replace("{task}", "t")
        return AnchoredSpec.model_validate(out)

    spec()  # the shipped one is fine
    with pytest.raises(ValueError, match="gives waive or void_when"):
        spec(void_when=[])
    with pytest.raises(ValueError, match="not both"):
        spec(waive=[{"label": "x", "when": raw["start"]["void_when"]}])
    with pytest.raises(ValueError, match="unknown field"):
        spec(void_when=[{"field": "nope", "in": ["already_at_destination"]}])
    with pytest.raises(ValueError, match="cannot take"):
        spec(void_when=[{"field": "start_state", "in": ["maybe"]}])
    # Only the rule final_state may void an episode.
    release = json.loads(generic.text("generic-release.v3.json"))
    release["start"] = raw["start"]
    release["question"] = "Did it land? {task}"
    with pytest.raises(
        ValueError, match="void_when goes with episode.rule final_state"
    ):
        AnchoredSpec.model_validate(release)


# --- the rule ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,end,result,is_undecided,reading,check",
    [
        # Not at the destination at the start: version 1's rule, unchanged.
        (
            "not_at_destination",
            "resting_at_destination",
            "success",
            False,
            "supported",
            "passed",
        ),
        ("not_at_destination", "elsewhere", "failure", False, "contradicted", "passed"),
        ("not_at_destination", "unclear", "failure", True, "unknown", "passed"),
        # Already there: no valid trial, whatever the final frames say.
        (
            "already_at_destination",
            "resting_at_destination",
            "failure",
            True,
            "already_satisfied_at_start",
            "voided",
        ),
        (
            "already_at_destination",
            "elsewhere",
            "failure",
            True,
            "already_satisfied_at_start",
            "voided",
        ),
        (
            "already_at_destination",
            "unclear",
            "failure",
            True,
            "already_satisfied_at_start",
            "voided",
        ),
        # Unreadable start: a success is not taken; a definite failure stays one.
        (
            "unclear",
            "resting_at_destination",
            "failure",
            True,
            "start_unclear",
            "unclear",
        ),
        ("unclear", "elsewhere", "failure", False, "contradicted", "unclear"),
        ("unclear", "unclear", "failure", True, "unknown", "unclear"),
        # No start answer (the first frames were not sent): version 1's rule.
        (None, "resting_at_destination", "success", False, "supported", "skipped"),
        (None, "elsewhere", "failure", False, "contradicted", "skipped"),
        (None, "unclear", "failure", True, "unknown", "skipped"),
    ],
)
def test_the_start_check_over_the_final_state_rule(
    start, end, result, is_undecided, reading, check
):
    spec = spec2()
    got, basis = outcome(spec, [answered(spec, end)], start and {"start_state": start})
    assert got == result
    assert anchored.undecided(got, basis) is is_undecided
    assert basis["final_reading"] == reading and basis["start_check"] == check
    assert basis["rule"] == "final_state"
    # A voided or unread start counts no valid event: the episode is no success.
    assert basis["valid_events"] == (1 if result == "success" else 0)
    flipped = check == "voided" or (check == "unclear" and reading == "start_unclear")
    # The final frames' own reading is kept when the start decided.
    assert ("end_reading" in basis) is flipped
    if flipped:
        assert basis["end_reading"] == answered(spec, end)["verdict"]


def test_a_voided_episode_is_never_a_success_and_the_v1_spec_ignores_the_start_answer():
    spec = spec2()
    ok = [answered(spec, "resting_at_destination")]
    # Without the start check this very answer is a success ...
    assert outcome(spec, ok)[0] == "success"
    # ... with it, the same final frames are not.
    assert outcome(spec, ok, {"start_state": "already_at_destination"})[0] == "failure"
    # Version 1 has no start check: a start answer changes nothing, and its
    # basis says nothing about one.
    one = AnchoredSpec.model_validate(generic.anchored_spec(TASK, V1))
    got, basis = outcome(
        one,
        [answered(one, "resting_at_destination")],
        {"start_state": "already_at_destination"},
    )
    assert got == "success" and "start_check" not in basis
    assert basis == outcome(one, [answered(one, "resting_at_destination")])[1]


def test_an_episode_with_no_final_frame_is_undecided_whatever_the_start_says():
    spec = spec2()
    got, basis = outcome(spec, [], {"start_state": "not_at_destination"})
    assert got == "failure" and anchored.undecided(got, basis)
    assert basis["final_reading"] is None
    got, basis = outcome(spec, [], {"start_state": "already_at_destination"})
    assert basis["final_reading"] == "already_satisfied_at_start"


def test_the_plan_s_estimate_counts_the_start_check():
    from levi.agent import planning

    v2 = anchored.dump(spec2())
    v1 = anchored.dump(AnchoredSpec.model_validate(generic.anchored_spec(TASK, V1)))
    assert "plus one start check per episode" in planning._extra_requests(v2)
    assert planning._extra_requests(v1) == ""


# --- one review through the real code ---------------------------------------------


def replies(fake, answers):
    def reply(payload):
        return completion(json.dumps(answers.pop(0)))

    fake.reply = reply


def review(dataset, fake, answers, episodes):
    """A final-state review with the start check over ``episodes`` of the test
    dataset, the fake model server ``fake`` answering ``answers`` in turn:
    (workbench, plan, run)."""
    from levi import catalog, service
    from levi.agent.runtime import Workbench

    camera = camera_dataset(dataset)
    entry = catalog.register(str(dataset))
    wb = Workbench(service.STATE)
    wb.store.put("providers", "vllm", config().model_dump())
    spec = generic.anchored_spec(TASK, V2)
    for view in [*spec["views"], *spec["start"]["views"]]:
        view["camera"] = camera
    context = TaskContext(
        repo_id=entry["id"],
        episodes=episodes,
        instruction="Judge each ending",
        provider="vllm",
        cameras=[camera],
        allow_media_egress=True,
        workflow={"kind": "review", "anchored": spec, "require_human_pilot": False},
        budget=Budget(max_calls=10, max_tokens=None, max_seconds=600),
    )
    run = wb.plan(context)
    approve(wb, run["id"], 1, "human")
    replies(fake, answers)
    assert wb.store.claim(run["id"], "owner")
    wb.execute(run["id"], "owner", pilot=False)
    return wb, run, wb.store.get("runs", run["id"])


def test_a_review_asks_the_first_frames_then_the_last_and_voids_a_started_episode(
    client,
    dataset,
    server,  # noqa: F811 - the fixture imported above
):
    from levi import service

    wb, run, result = review(
        dataset,
        server,
        [
            # Episode 0: the object is on the bread at the first frame (and, as
            # the final frames show, still there). Episode 1: it was not, and
            # now is.
            {"start_state": "already_at_destination"},
            {"object_state": "resting_at_destination", "stable": "yes"},
            {"start_state": "not_at_destination"},
            {"object_state": "resting_at_destination", "stable": "yes"},
        ],
        [0, 1],
    )
    estimate = run["plan"]["estimate"]
    assert "plus one start check per episode" in estimate["basis"]
    # The start check and the final question: two requests an episode.
    assert estimate["minimum_requests"] == 2 * len(run["context"]["episodes"])
    assert result["status"] == "waiting_for_review", result["reason"]
    chats = server.chats()
    assert len(chats) == 4 and result["requests"] == 4
    for n, chat in enumerate(chats):
        (message,) = chat["messages"]
        kinds = [p["type"] for p in message["content"]]
        schema = chat["response_format"]["json_schema"]["schema"]
        text = message["content"][-1]["text"]
        assert TASK in text and "{task}" not in text and CONVENTION in text
        if n % 2 == 0:  # the start check: two images, its own answer fields
            assert kinds == ["image_url"] * 2 + ["text"]
            assert list(schema["properties"]) == ["start_state"]
        else:
            assert kinds == ["image_url"] * 10 + ["text"]
            assert list(schema["properties"]) == ["object_state", "stable"]
    first = wb.store.get("anchored", f"{run['id']}:0")
    assert first["outcome"] == "failure"
    assert first["start"]["answer"] == {"start_state": "already_at_destination"}
    assert first["start"]["void"]["reading"] == "supported"
    assert first["start"]["waive"] == []
    assert [f["frame_index"] for f in first["start"]["frames"]] == [0, 0]
    assert first["basis"]["final_reading"] == "already_satisfied_at_start"
    assert first["basis"]["start_check"] == "voided"
    assert first["basis"]["end_reading"] == "supported"
    assert anchored.undecided(first["outcome"], first["basis"])
    second = wb.store.get("anchored", f"{run['id']}:1")
    assert second["outcome"] == "success" and second["basis"]["start_check"] == "passed"
    assert not anchored.undecided(second["outcome"], second["basis"])
    # What a person sees in the review queue.
    change = wb.store.get("changes", result["changes"])
    by_episode = {p["episode_index"]: p for p in change["proposals"]}
    assert by_episode[0]["outcome"] == "failure"
    assert (
        "already at the destination in the first frames" in by_episode[0]["uncertainty"]
    )
    assert "not a valid trial" in by_episode[0]["evidence_note"]
    assert "start (start_state=already_at_destination)" in by_episode[0]["content"]
    start_evidence = {f["evidence_id"] for f in first["start"]["frames"]}
    assert start_evidence <= set(by_episode[0]["evidence_ids"])
    assert by_episode[1]["outcome"] == "success" and not by_episode[1]["uncertainty"]
    assert not list(service.STATE.rglob("outcomes"))


def test_a_review_with_an_unreadable_start_says_so_to_the_person_who_reviews_it(
    client,
    dataset,
    server,  # noqa: F811 - the fixture imported above
):
    wb, run, result = review(
        dataset,
        server,
        [
            {"start_state": "unclear"},
            {"object_state": "resting_at_destination", "stable": "yes"},
        ],
        [0],
    )
    assert result["status"] == "waiting_for_review", result["reason"]
    record = wb.store.get("anchored", f"{run['id']}:0")
    assert record["outcome"] == "failure"
    assert record["start"]["void"]["reading"] == "unknown"
    assert record["basis"]["final_reading"] == "start_unclear"
    assert record["basis"]["start_check"] == "unclear"
    assert record["basis"]["end_reading"] == "supported"
    assert anchored.undecided(record["outcome"], record["basis"])
    (proposal,) = wb.store.get("changes", result["changes"])["proposals"]
    assert proposal["outcome"] == "failure"
    # The reviewer is told which input was not read, and is shown it.
    assert proposal["uncertainty"] == (
        "the first frames did not show whether the object was already at the "
        "destination"
    )
    assert "not a valid trial" not in proposal["evidence_note"]
    assert {f["evidence_id"] for f in record["start"]["frames"]} <= set(
        proposal["evidence_ids"]
    )


# --- the live settings and the background worker --------------------------------------


def load(tmp_path, toml):
    path = tmp_path / "live.toml"
    path.write_text(toml)
    return live_config.load(path)


def test_the_new_spec_runs_review_only_and_is_not_the_default(tmp_path):
    c = load(tmp_path, f"[pipeline]\ntemporal = false\nanchored_spec = '{V2}'\n")
    assert c.pipeline.anchored_spec == V2
    assert generic.manifest(c) == {V2: generic.sha256(V2)}
    # min_valid above 1 is refused for it as for version 1 (one event).
    with pytest.raises(ValueError, match="cannot be used with"):
        load(
            tmp_path,
            f"[pipeline]\nanchored_spec = '{V2}'\nanchored_min_valid = 2\n",
        )
    # The default stays version 1 of the release review; the online spec v1.
    d = live_config.Config()
    assert (d.pipeline.anchored_spec, d.online.spec) == ("generic-release.v1.json", V1)


def use_final_v2(e):
    e.config.pipeline.temporal = False
    e.config.pipeline.anchored_spec = V2
    e.config.validate()


def test_the_worker_voids_an_episode_whose_object_started_at_the_destination(env):  # noqa: F811
    """The model says the object is on the destination at the first frame and
    still there at the end: the fake model answers every question the same
    way, so version 1 would call this a success. The verdict is a failure
    that is undecided, and says why."""
    from levi.live import api, stats

    e = env(answers={"start_state": "already_at_destination"})
    use_final_v2(e)
    e.rollouts.write(0)
    e.run()
    asked = [p for p in e.fake.calls if p.get("max_completion_tokens") != 1]
    assert len(asked) == 2  # the start check and the final frames, once each
    row = e.state()["demos"]["demo_0000"]
    verdict = row["verdict"]
    assert row["state"] == "done"
    assert (verdict["outcome"], verdict["undecided"]) == ("failure", True)
    assert verdict["valid_events"] == 0 and verdict["events"] == 1
    assert verdict["basis"]["final_reading"] == "already_satisfied_at_start"
    assert verdict["basis"]["start_check"] == "voided"
    assert verdict["basis"]["end_reading"] == "supported"
    assert (verdict["spec"], verdict["spec_version"]) == ("generic-final", 2)
    assert verdict["rule"] == "final_state" and verdict["evaluated"] is False
    (record,) = [r for r in e.records("anchored")]
    assert record["start"]["answer"] == {"start_state": "already_at_destination"}
    assert record["outcome"] == "failure"
    # The page and the statistics read it as an undecided failure, and say why:
    # the statistics can count how often the start check voids or cannot read.
    assert stats.agent_of(verdict) == "undecided"
    page = api._demo_row("demo_0000", row)["verdict"]
    assert page["undecided"] is True
    assert (page["final_reading"], page["start_check"]) == (
        "already_satisfied_at_start",
        "voided",
    )
    (stat,) = stats.read(e.ws / "live")
    assert stat["result"]["verdict"]["final_reading"] == "already_satisfied_at_start"
    assert stat["result"]["verdict"]["start_check"] == "voided"
    assert stat["model"]["requests"]["review"] == 2  # both questions are counted
    assert not list(e.ws.rglob("outcomes"))  # never a label


def test_the_worker_keeps_a_success_when_the_start_was_not_at_the_destination(env):  # noqa: F811
    e = env(answers={"start_state": "not_at_destination"})
    use_final_v2(e)
    e.rollouts.write(0)
    e.run()
    verdict = e.state()["demos"]["demo_0000"]["verdict"]
    assert (verdict["outcome"], verdict["undecided"]) == ("success", False)
    assert verdict["valid_events"] == 1
    assert verdict["basis"]["final_reading"] == "supported"
    assert verdict["basis"]["start_check"] == "passed"
    assert "end_reading" not in verdict["basis"]


def test_the_worker_does_not_let_an_unreadable_start_stand_for_a_success(env):  # noqa: F811
    e = env(answers={"start_state": "unclear"})
    use_final_v2(e)
    e.rollouts.write(0)
    e.run()
    verdict = e.state()["demos"]["demo_0000"]["verdict"]
    assert (verdict["outcome"], verdict["undecided"]) == ("failure", True)
    assert verdict["basis"]["final_reading"] == "start_unclear"
    assert verdict["basis"]["start_check"] == "unclear"
    assert verdict["valid_events"] == 0
