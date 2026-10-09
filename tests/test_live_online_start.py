"""The online judgement's optional start frames (interface C5, revision 2): a
spec with a start check (``generic-final.v2``) lists the first-frame views
under ``start_views`` (never under ``views``, which a client of revision 1
reads strictly); a request that sends them gets the start check asked first,
one that does not is judged by the final frames alone (``start_check:
skipped``). The strict request, the independence of the model's input from
anything the operator or the evaluation knows, and the relayed result read
back by the mirror. A protocol stand-in replaces the model; no GPU."""

import base64
import json

import pytest
from test_live_online import (
    JPEG,
    Ask,
    Gpu,
    call,
    cfg,
    images,
    judge_with,
    post,
    request,
)

from levi.live import config as live_config
from levi.live import criteria, fakevlm, generic, online

V1 = "generic-final.v1.json"
V2 = "generic-final.v2.json"
TASK = "put the red cup on the plate"
START_ROLES = ("start_side", "start_wrist")


def start_images(skip=(), extra=()):
    out = [
        {
            "role": role,
            "offset_s": 0.0,
            "step": 1,
            "jpeg_b64": base64.b64encode(JPEG).decode(),
        }
        for role in START_ROLES
        if role not in skip
    ]
    return out + list(extra)


def with_start(**over):
    body = request(**over)
    body["images"] = body["images"] + start_images()
    return body


class AskBoth(Ask):
    """A model stand-in for the two questions: the start check is told apart
    by its type (a ``StartCheck``, which has ``void_when``); ``order`` keeps
    which was asked first."""

    def __init__(self, start="not_at_destination", **kw):
        super().__init__(**kw)
        self.start = start
        self.probes = []
        self.order = []

    def __call__(self, spec, folder, names):
        if hasattr(spec, "void_when"):
            self.order.append("start")
            self.probes.append(
                {
                    "question": spec.question,
                    "names": list(names),
                    "files": [(folder / n).read_bytes() for n in names],
                    "schema": spec.answer_schema(),
                    "max_output_tokens": spec.max_output_tokens,
                }
            )
            return json.dumps({"start_state": self.start}), {
                "reported_tokens": 100,
                "prompt_tokens": 90,
            }
        self.order.append("end")
        return super().__call__(spec, folder, names)


def judge2(tmp_path, ask=None, **online_over):
    return judge_with(cfg(tmp_path, spec=V2, **online_over), ask=ask or AskBoth())


# --- the spec route ---------------------------------------------------------------------


def test_the_spec_route_lists_the_start_views_apart_from_the_views(tmp_path):
    two = online.spec_answer(judge2(tmp_path).spec, live_config.Config())
    one = online.spec_answer(
        judge_with(cfg(tmp_path / "v1")).spec,
        live_config.Config(),
    )
    # What a client of revision 1 reads is exactly what it read: its schema
    # string, its two roles with their offsets (it rejects any other role) ...
    assert two["schema"] == one["schema"] == "levi.online.judge.spec.v1"
    assert two["views"] == one["views"]
    assert [v["role"] for v in two["views"]] == ["side", "wrist"]
    # ... and the start frames are elsewhere, named, anchored at the first frame.
    assert two["revision"] == one["revision"] == 2
    assert "start_views" not in one
    assert two["start_views"] == [
        {"role": "start_side", "anchor": "start", "offsets_seconds": [0.0]},
        {"role": "start_wrist", "anchor": "start", "offsets_seconds": [0.0]},
    ]
    assert (two["spec_id"], two["spec_version"]) == ("generic-final", 2)


def test_the_spec_route_over_http_carries_the_start_views(tmp_path):
    point = online.Endpoint(cfg(tmp_path, spec=V2), Gpu(), ask=AskBoth())
    assert point.start()
    try:
        code, body = call(point, "GET", "/v1/judge/spec")
    finally:
        point.stop()
    assert code == 200 and [v["role"] for v in body["start_views"]] == list(START_ROLES)


# --- an old client: no start frames ---------------------------------------------------------


@pytest.mark.parametrize(
    "end,outcome,is_undecided,final",
    [
        ("resting_at_destination", "success", False, "supported"),
        ("elsewhere", "failure", False, "contradicted"),
        ("unclear", "failure", True, "unknown"),
    ],
)
def test_a_request_without_start_frames_is_judged_by_version_1_s_rule(
    tmp_path, end, outcome, is_undecided, final
):
    ask = AskBoth(answer={"object_state": end, "stable": "yes"})
    code, body = post(judge2(tmp_path, ask), request())  # the revision-1 request
    assert code == 200 and body["status"] == "ok", body
    assert (body["outcome"], body["undecided"]) == (outcome, is_undecided)
    assert body["final_reading"] == final
    assert body["start_check"] == "skipped" and body["start_answer"] == {}
    # The start check was not asked: one request to the model, the final one.
    assert ask.order == ["end"] and ask.probes == []
    assert len(ask.calls[0]["names"]) == 10


# --- a client that sends them ------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,end,outcome,is_undecided,final,check",
    [
        (
            "not_at_destination",
            "resting_at_destination",
            "success",
            False,
            "supported",
            "passed",
        ),
        ("not_at_destination", "elsewhere", "failure", False, "contradicted", "passed"),
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
            "unclear",
            "resting_at_destination",
            "failure",
            True,
            "start_unclear",
            "unclear",
        ),
        ("unclear", "elsewhere", "failure", False, "contradicted", "unclear"),
    ],
)
def test_the_start_frames_decide_the_episode_as_the_background_review_does(
    tmp_path, start, end, outcome, is_undecided, final, check
):
    ask = AskBoth(start=start, answer={"object_state": end, "stable": "yes"})
    code, body = post(judge2(tmp_path, ask), with_start())
    assert code == 200 and body["status"] == "ok", body
    assert (body["outcome"], body["undecided"]) == (outcome, is_undecided)
    assert body["final_reading"] == final and body["start_check"] == check
    assert body["start_answer"] == {"start_state": start}
    # The reading of the final frames' own conditions is not rewritten.
    assert body["reading"] == (
        "supported" if end == "resting_at_destination" else "contradicted"
    )
    assert body["answer"] == {"object_state": end, "stable": "yes"}
    # The start check is asked first, with the two first frames and its own
    # schema; the final question keeps its ten images.
    assert ask.order == ["start", "end"]
    (probe,) = ask.probes
    assert len(probe["names"]) == 2 and probe["files"] == [JPEG, JPEG]
    assert list(probe["schema"]["properties"]) == ["start_state"]
    assert probe["max_output_tokens"] == 60
    assert TASK in probe["question"] and "{task}" not in probe["question"]
    assert len(ask.calls[0]["names"]) == 10
    assert (body["tokens"], body["prompt_tokens"]) == (612, 570)  # both questions


def test_a_voided_episode_is_never_a_success_even_when_the_final_frames_say_so(
    tmp_path,
):
    ask = AskBoth(start="already_at_destination")
    body = post(judge2(tmp_path, ask), with_start())[1]
    assert ask.calls[0]["names"]  # the final frames were read ...
    assert body["reading"] == "supported" and body["answer"]["object_state"] == (
        "resting_at_destination"
    )
    assert (
        body["outcome"] == "failure" and body["undecided"] is True
    )  # ... and not trusted


def test_an_invalid_start_answer_is_an_error_not_a_verdict(tmp_path):
    class Bad(AskBoth):
        def __call__(self, spec, folder, names):
            if hasattr(spec, "void_when"):
                return json.dumps({"start_state": "maybe"}), {}
            return super().__call__(spec, folder, names)

    code, body = post(judge2(tmp_path, Bad()), with_start())
    assert code == 200 and body["status"] == "error"
    assert body["reason"].startswith("invalid_answer: start check:")
    assert body["outcome"] is None and body["start_check"] is None


def test_the_log_counts_every_image_and_names_the_start_check(tmp_path):
    judge = judge2(tmp_path)
    post(judge, with_start())
    post(judge, request())
    voided, old = online.read_log(judge.config.live_dir)
    assert (voided["images"], old["images"]) == (12, 10)
    assert (voided["start_check"], old["start_check"]) == ("passed", "skipped")
    assert "start_answer" not in voided and TASK not in json.dumps(voided)


# --- the strict request --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,words",
    [
        # Some of the start frames, not all: a mistake, said so.
        (
            request(images=images() + start_images(skip={"start_wrist"})),
            "start images missing for start_wrist at 0.0 s",
        ),
        (
            request(images=images() + start_images(extra=start_images()[:1])),
            "a second image for start_side at 0.0 s",
        ),
        (
            request(
                images=images()
                + start_images(skip={"start_side", "start_wrist"})
                + [{"role": "start_side", "offset_s": -1.0, "jpeg_b64": "AA=="}]
            ),
            "the spec shows start_side at [0.0] s, not at -1.0 s",
        ),
        # No new key is admitted anywhere: the start frames are images of the
        # same contract, named by role.
        (
            request(
                images=images()
                + [{**start_images()[0], "anchor": "start"}, start_images()[1]]
            ),
            "images[10] has keys the contract does not define: anchor",
        ),
        (
            with_start(start_state="already_at_destination"),
            "does not define: start_state",
        ),
        (with_start(operator_outcome="failure"), "operator or evaluation data"),
        (with_start(outcome="success"), "operator or evaluation data"),
        (
            with_start(episode={"demo": "demo_1", "operator_label": "failure"}),
            "episode has keys",
        ),
    ],
)
def test_the_request_stays_strict_with_the_start_frames(tmp_path, body, words):
    ask = AskBoth()
    code, answer = post(judge2(tmp_path, ask), body)
    assert code == 422, answer
    assert answer["status"] == "error" and words in answer["reason"], answer["reason"]
    assert answer["outcome"] is None and ask.order == []  # the model was never asked


def test_a_service_whose_spec_has_no_start_check_refuses_start_frames(tmp_path):
    ask = Ask()
    judge = judge_with(cfg(tmp_path), ask=ask)  # generic-final.v1
    code, answer = post(judge, with_start())
    assert (
        code == 422
        and "role 'start_side' is not one of the spec's views" in answer["reason"]
    )
    assert ask.calls == []


def test_start_frames_alone_do_not_stand_in_for_the_final_ones(tmp_path):
    body = request(images=start_images())
    code, answer = post(judge2(tmp_path), body)
    assert code == 422 and "images missing for side at -3.0 s" in answer["reason"]


# --- nothing but the images and the questions reaches the model ------------------------------------


def test_the_model_requests_hold_the_images_and_the_questions_and_no_metadata(tmp_path):
    server, fake, port = fakevlm.serve(
        0,
        fakevlm.Fake(
            answers={
                "start_state": "not_at_destination",
                "object_state": "resting_at_destination",
                "stable": "yes",
            }
        ),
    )
    try:
        judge = online.Judge(cfg(tmp_path, spec=V2), Gpu(port))  # the real model path
        code, body = post(judge, with_start())
        assert code == 200 and body["status"] == "ok", body
        assert body["outcome"] == "success" and body["start_check"] == "passed"
        assert body["prompt_tokens"] == 800  # two requests of 400
        payloads = list(fake.calls)
    finally:
        server.shutdown()
    assert len(payloads) == 2
    for payload in payloads:
        dumped = json.dumps(payload)
        for marker in ("RUN-MARKER-7f3a", "demo_0042", "pi05_group_marker", "cups"):
            assert marker not in dumped
        assert "operator" not in dumped.lower()
    start, end = payloads
    kinds = lambda p: [x["type"] for x in p["messages"][0]["content"]]
    assert kinds(start) == ["image_url"] * 2 + ["text"]
    assert kinds(end) == ["image_url"] * 10 + ["text"]
    questions = generic.anchored_spec(TASK, V2)
    assert start["messages"][0]["content"][-1]["text"] == questions["start"]["question"]
    assert end["messages"][0]["content"][-1]["text"] == questions["question"]
    assert start["response_format"]["json_schema"]["schema"]["required"] == [
        "start_state"
    ]
    assert end["response_format"]["json_schema"]["schema"]["required"] == [
        "object_state",
        "stable",
    ]


# --- over HTTP ------------------------------------------------------------------------------------------


def test_a_judgement_with_start_frames_over_http(tmp_path):
    ask = AskBoth(start="already_at_destination")
    point = online.Endpoint(cfg(tmp_path, spec=V2), Gpu(), ask=ask)
    assert point.start()
    try:
        code, body = call(point, "POST", "/v1/judge", with_start())
        old_code, old = call(point, "POST", "/v1/judge", request())
    finally:
        point.stop()
    assert code == old_code == 200
    assert body["schema"] == "levi.online.judge.result.v1"
    assert (body["outcome"], body["undecided"], body["start_check"]) == (
        "failure",
        True,
        "voided",
    )
    assert old["outcome"] == "success" and old["start_check"] == "skipped"


# --- the configuration ----------------------------------------------------------------------------------


def test_the_new_spec_is_accepted_and_the_default_stays_version_1(tmp_path):
    assert live_config.Config().online.spec == V1
    assert live_config.online_spec_problems(V2) == []
    cfg(tmp_path, spec=V2)  # validates
    # A release spec is still not a final-state spec, and vetoes still refused.
    assert (
        "not a final-state spec"
        in live_config.online_spec_problems("generic-release.v3.json")[0]
    )


# --- the relayed result read back -------------------------------------------------------------------------


def relayed_v2(start="voided", final="already_satisfied_at_start", **more):
    body = online.result(
        "ok",
        request_id="abc123",
        spec={"id": "generic-final", "version": 2},
        model="qwen3.8-27b",
        elapsed=2.5,
        answer={"object_state": "resting_at_destination", "stable": "yes"},
        reading="supported",
        outcome="failure",
        undecided=True,
        tokens=900,
        prompt_tokens=850,
        final_reading=final,
        start_check=start,
        start_answer={"start_state": "already_at_destination"},
        task_rewritten="task_text",
    )
    return {
        **body,
        "source": "online",
        "received_at": 1790000002.5,
        "timing": "during_run",
        **more,
    }


def test_the_mirror_keeps_the_start_decision_in_the_verdict():
    found = criteria.agent_label({"eval": {"agent_label": relayed_v2()}})
    verdict = found["verdict"]
    assert found["status"] == "ok"
    assert (verdict["outcome"], verdict["undecided"]) == ("failure", True)
    assert (verdict["events"], verdict["valid_events"]) == (1, 0)
    assert verdict["basis"]["final_reading"] == "already_satisfied_at_start"
    assert verdict["basis"]["start_check"] == "voided"
    assert verdict["spec_version"] == 2 and verdict["task_rewritten"] == "task_text"
    # An ordinary reading (revision 1 keys only) is read as it always was.
    plain = {
        k: v
        for k, v in relayed_v2().items()
        if k not in ("final_reading", "start_check", "task_rewritten")
    }
    plain["reading"] = "contradicted"
    got = criteria.agent_label({"eval": {"agent_label": plain}})["verdict"]
    assert got["basis"] == {
        "valid_events": 0,
        "min_valid": 1,
        "rule": "final_state",
        "final_reading": "contradicted",
    }
    assert "task_rewritten" not in got


@pytest.mark.parametrize(
    "more",
    [
        # A start-based reading is a failure that is undecided, nothing else.
        {"outcome": "success", "undecided": False},
        {"undecided": False},
    ],
)
def test_a_start_decision_that_does_not_hold_together_gives_no_verdict(more):
    found = criteria.agent_label({"eval": {"agent_label": relayed_v2(**more)}})
    assert found["status"] == "error" and found["verdict"] is None


def test_the_constants_the_mirror_reads_are_the_rule_s():
    """The mirror (light, no pydantic) lists the values the rule can produce."""
    from levi.agent import anchored

    assert set(criteria.START_READINGS) == {
        anchored.ALREADY_AT_START,
        anchored.START_UNCLEAR,
    }
    spec = anchored.AnchoredSpec.model_validate(generic.anchored_spec(TASK, V2))
    seen = set()
    for start in ("already_at_destination", "not_at_destination", "unclear", None):
        for end in ("resting_at_destination", "elsewhere"):
            answer = {"object_state": end, "stable": "yes"}
            checks, verdict = anchored.judge(spec, answer)
            event = {
                "answer": answer,
                "checks": checks,
                "verdict": verdict,
                "valid": verdict == "supported",
            }
            _, basis = anchored.outcome(
                spec, [event], start and {"start_state": start}
            )
            seen.add(basis["start_check"])
    assert seen == set(criteria.START_CHECKS)


def test_an_unknown_reading_or_start_check_is_dropped_not_trusted():
    odd = relayed_v2(final="success!", start="whatever", task_rewritten="x")
    got = criteria.agent_label({"eval": {"agent_label": odd}})["verdict"]
    assert got["basis"]["final_reading"] == "supported"  # the plain reading
    assert "start_check" not in got["basis"] and "task_rewritten" not in got


def test_an_online_episode_counts_a_request_for_each_question_asked():
    """With the background labelling off the statistics record is built from the
    relayed result: the start check is a second model request, and the reason
    an episode is undecided is in the record (so its rate can be counted)."""
    from levi.live import stats

    def record(label, **more):
        found = criteria.agent_label({"eval": {"agent_label": label}})
        row = {
            "state": "done",
            "completed_at": 1790000000.0,
            "verdict": found["verdict"],
            "online": {"status": found["status"], "usage": found["usage"]},
        }
        return online.stats_record(live_config.Config(), "ds", "demo_0001", row, 2e9)

    voided = record(relayed_v2())
    assert voided["model"]["requests"]["review"] == 2
    assert voided["result"]["verdict"]["final_reading"] == "already_satisfied_at_start"
    assert voided["result"]["verdict"]["start_check"] == "voided"
    unclear = record(relayed_v2(start="unclear", final="start_unclear"))
    assert unclear["model"]["requests"]["review"] == 2
    assert unclear["result"]["verdict"]["start_check"] == "unclear"
    passed = record(
        relayed_v2(
            start="passed", final="supported", outcome="success", undecided=False
        )
    )
    assert passed["model"]["requests"]["review"] == 2
    assert passed["result"]["verdict"]["final_reading"] == "supported"
    # Not asked (a client of revision 1, or a spec with no start check): one.
    skipped = record(
        relayed_v2(
            start="skipped", final="supported", outcome="success", undecided=False
        )
    )
    assert skipped["model"]["requests"]["review"] == 1
    assert skipped["result"]["verdict"]["start_check"] == "skipped"
    plain = {
        k: v
        for k, v in relayed_v2(final="supported").items()
        if k not in ("start_check", "final_reading")
    }
    plain.update(outcome="success", undecided=False)
    old = record(plain)
    assert old["model"]["requests"]["review"] == 1
    assert "start_check" not in old["result"]["verdict"]
    # The statistics reader still folds these records.
    assert stats.summarize([voided, old])["agreement"]["pairs"] == 0
