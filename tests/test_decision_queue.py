"""Escalation queue: append-only, replayable, idempotent, response contract (U5, U12, U15, U16, U20, U21)."""

import dataclasses
import json

import pytest

from levi.decision import leakcheck, queue
from levi.decision.contracts import (
    RESPONSE_CONTRACT,
    Context,
    EscalationRequest,
    LabelTier,
    State,
)

SCHEMA = {
    "type": "object",
    "properties": {"closed": {"type": "boolean"}},
    "required": ["closed"],
    "additionalProperties": False,
}
OFFLINE = Context.OFFLINE
DENY = leakcheck.Denylist.build(["ep_confirm_001"], [])


def req(rid="r1a2b3c4d5", text="Is the gripper closed?", **kw):
    base = {
        "request_id": rid,
        "question_kind": "boundary",
        "spec_id": "spec-v1",
        "messages": ({"role": "user", "content": text},),
        "response_schema": SCHEMA,
    }
    base.update(kw)
    return EscalationRequest(**base)


def resp(request, a=True, b=True, adj=None, answer=None, **over):
    panel = {"reviewer_a": {"closed": a}, "reviewer_b": {"closed": b}}
    if adj is not None:
        panel["adjudicator"] = {"closed": adj}
    decided = {"closed": adj if adj is not None else a}
    body = {
        "contract": RESPONSE_CONTRACT,
        "request_id": request.request_id,
        "answer": answer if answer is not None else decided,
        "panel": panel,
        "tokens": [
            {
                "role": "reviewer_a",
                "input_new": 10,
                "output": 5,
                "cache_read": 100,
                "cache_write": 0,
                "exact": True,
                "source_code": "claude_jsonl.last_usage:exact",
            }
        ],
        "label_tier": "agent-reviewed",
    }
    body.update(over)
    return json.dumps(body)


@pytest.fixture
def q(tmp_path):
    return queue.Queue(tmp_path / "q.jsonl", DENY, max_attempts=3)


# request contract: the local answer is not part of it
def test_request_has_no_local_answer_field_and_the_wire_is_the_teacher_view():
    names = {f.name for f in dataclasses.fields(EscalationRequest)}
    assert not {
        n for n in names if "local" in n or "confidence" in n or n.startswith("p_")
    }
    wire = queue.to_wire(req())
    assert set(wire) == queue.WIRE_KEYS
    assert "question_kind" not in wire  # the panel sees spec_id, not the routing kind
    assert not [
        k for k in wire if "local" in k or "split" in k or "item" in k or "episode" in k
    ]


def test_a_request_whose_extra_field_would_carry_a_local_answer_has_nowhere_to_go():
    with pytest.raises(TypeError):
        EscalationRequest(
            request_id="r",
            question_kind="k",
            spec_id="s",
            messages=(),
            response_schema={},
            local_answer="true",
        )


def test_enqueue_is_idempotent_on_the_request_sha256(q):
    sha = q.enqueue(req(), OFFLINE)
    assert q.enqueue(req(), OFFLINE) == sha == queue.request_sha256(req())
    assert len(q.path.read_text().splitlines()) == 1
    assert q.enqueue(req(text="Is the gripper open?"), OFFLINE) != sha
    assert len(q.pending()) == 2


def test_the_queue_is_append_only_and_replays_from_the_file(q):
    sha = q.enqueue(req(), OFFLINE)
    q.submit(sha, "not json")
    before = q.path.read_text()
    again = queue.Queue(
        q.path, DENY, max_attempts=3
    )  # a fresh object: state comes from the file
    assert (
        again.status(sha).attempts == 1
        and again.status(sha).state == "pending_external"
    )
    again.submit(sha, resp(req()))
    assert again.path.read_text().startswith(before)


# U5: unavailable external -> waits; no label, no local answer, no person
def test_unavailable_panel_leaves_the_item_pending_and_writes_nothing_else(q):
    sha = q.enqueue(req(), OFFLINE)
    q.enqueue(req(rid="r2"), OFFLINE)
    calls = []

    def down(wire):
        calls.append(wire["request_id"])
        raise TimeoutError

    results, unavailable = q.drain(down)
    assert (
        unavailable and results == [] and calls == ["r1a2b3c4d5"]
    )  # stops at the first failure
    assert q.status(sha).state == State.PENDING_EXTERNAL.value
    assert q.drain(lambda w: None)[1] is True
    text = q.path.read_text()
    assert "label" not in text and "local" not in text and "human" not in text
    assert len(text.splitlines()) == 2  # only the two enqueue events
    assert q.status(sha).response is None


def test_recovered_panel_drains_in_order_and_the_first_answer_wins(q):
    s1, s2 = q.enqueue(req(), OFFLINE), q.enqueue(req(rid="r2"), OFFLINE)
    results, unavailable = q.drain(
        lambda wire: resp(queue._from_wire(wire, "boundary"))
    )
    assert (
        not unavailable and [r[0].state for r in results] == ["answered_external"] * 2
    )
    assert q.pending() == []
    first = q.status(s1).response.answer
    late, parsed = q.submit(
        s1, resp(req(), a=False, b=False)
    )  # a later, different answer
    assert parsed is None and late.response.answer == first == {"closed": True}
    assert q.status(s2).response.label_tier is LabelTier.AGENT_REVIEWED


# U15: a response that breaks the contract is not an answer
@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda r: resp(r, answer={"closed": "yes"}), "answer_schema"),
        (lambda r: resp(r, answer={"closed": True, "extra": 1}), "answer_schema"),
        (lambda r: resp(r, request_id="other"), "request_id"),
        (
            lambda r: resp(r, contract="levi.decision.escalation_response.v0"),
            "contract",
        ),
        (lambda r: resp(r, label_tier="human"), "label_tier"),
        (
            lambda r: '{"answer": {"closed": true}, "answer": {"closed": false}}',
            "not_json",
        ),
        (lambda r: '{"answer": NaN}', "not_json"),
        (lambda r: "[" * 100 + "]" * 100, "not_json"),
        (lambda r: resp(r, panel={"reviewer_a": {"closed": True}}), "panel"),
    ],
)
def test_invalid_response_is_not_an_answer(q, mutate, code):
    r = req()
    sha = q.enqueue(r, OFFLINE)
    status, parsed = q.submit(sha, mutate(r))
    assert code in parsed.problems
    assert (
        status.state == "pending_external"
        and status.response is None
        and status.attempts == 1
    )


def test_invalid_responses_requeue_then_fail_and_never_use_the_local_answer(q):
    r = req()
    sha = q.enqueue(r, OFFLINE)
    for n in (1, 2):
        assert q.submit(sha, "garbage")[0].state == "pending_external"
        assert q.status(sha).attempts == n
    final, _ = q.submit(sha, "garbage")
    assert final.state == queue.FAILED and final.attempts == 3 and q.pending() == []
    # failed is terminal: a good answer afterwards is ignored, and no label or local value exists
    assert q.submit(sha, resp(r))[1] is None and q.status(sha).state == queue.FAILED
    assert "local" not in q.path.read_text()


# U20: two reviewers who disagree need an adjudicator, and the answer is the adjudicator's
def test_disagreement_needs_an_adjudicator_and_the_answer_is_theirs(q):
    r = req()
    sha = q.enqueue(r, OFFLINE)
    _, p = q.submit(sha, resp(r, a=True, b=False, answer={"closed": True}))
    assert "adjudication_missing" in p.problems
    _, p = q.submit(
        sha, resp(r, a=True, b=False, adj=False, answer={"closed": True})
    )  # took A's answer
    assert "answer_not_panel_result" in p.problems
    status, p = q.submit(sha, resp(r, a=True, b=False, adj=False))
    assert p.problems == () and status.response.answer == {"closed": False}


def test_reviewer_answers_must_fit_the_schema_too(q):
    r = req()
    sha = q.enqueue(r, OFFLINE)
    bad = json.loads(resp(r))
    bad["panel"]["reviewer_b"] = {"closed": "maybe"}
    _, p = q.submit(sha, json.dumps(bad))
    assert "reviewer_schema" in p.problems


def test_token_rows_survive_a_rejected_response_and_unknown_is_not_zero(q):
    r = req()
    sha = q.enqueue(r, OFFLINE)
    body = json.loads(resp(r, answer={"closed": "x"}))
    body["tokens"] = [
        {
            "role": "reviewer_a",
            "input_new": 7,
            "output": None,
            "cache_read": -1,
            "exact": True,
        }
    ]
    _, p = q.submit(sha, json.dumps(body))
    ((row, code),) = p.token_rows
    assert (row.input_new, row.output, row.cache_read, row.cache_write) == (
        7,
        None,
        None,
        None,
    )
    assert row.exact is False and code is None


# unsupported schema keyword: fail closed, never accept unchecked answers
def test_unsupported_schema_keyword_is_refused_at_enqueue(q):
    with pytest.raises(queue.UnsupportedSchema):
        q.enqueue(req(response_schema={"oneOf": [{"type": "string"}]}), OFFLINE)
    assert not q.path.exists()


def test_schema_conforms_basics():
    assert queue.conforms(1, {"type": "integer"}) and not queue.conforms(
        True, {"type": "integer"}
    )
    assert not queue.conforms(1, {"enum": [True]}) and queue.conforms(
        "a", {"enum": ["a", "b"]}
    )
    assert queue.conforms([{"closed": True}], {"type": "array", "items": SCHEMA})


# P6 inside the queue: the gate runs before anything is stored or sent
def test_leaky_request_is_refused_at_enqueue_and_again_before_send(tmp_path):
    q = queue.Queue(tmp_path / "q.jsonl", DENY)
    with pytest.raises(leakcheck.LeakFound):
        q.enqueue(req(signals={"clip": "ep_confirm_001"}), OFFLINE)
    assert not q.path.exists()
    sha = q.enqueue(req(), OFFLINE)
    grown = queue.Queue(
        q.path, leakcheck.Denylist.build(["ep_confirm_001", "Is the gripper"], [])
    )
    sent = []
    with pytest.raises(leakcheck.LeakFound):
        grown.drain(lambda w: sent.append(w))
    assert sent == [] and grown.status(sha).state == "pending_external"


# U16: pending and failed stay in the denominator
def test_pending_and_failed_stay_in_the_denominator(q):
    a, b, _c = (q.enqueue(req(rid=f"r{i}", text=f"q{i}"), OFFLINE) for i in range(3))
    q.submit(a, resp(q.request(a)))
    for _ in range(3):
        q.submit(b, "garbage")
    n = q.counts()
    assert (n["answered_external"], n["external_failed"], n["pending_external"]) == (
        1,
        1,
        1,
    )
    assert n["in_denominator"] == n["total"] == 3


# U21: reuse of the teacher's answer needs the same request sha256
def test_reuse_requires_the_identical_request():
    r = req()
    sha = queue.request_sha256(r)
    assert queue.reuse_reference_answer(r, sha, {"closed": True}) == {"closed": True}
    assert (
        queue.reuse_reference_answer(
            req(text="Is the gripper closed!"), sha, {"closed": True}
        )
        is None
    )


def test_a_line_that_is_not_an_event_refuses(q):
    q.enqueue(req(), OFFLINE)
    with q.path.open("a") as f:
        f.write('{"schema": "x"}\n')
    with pytest.raises(queue.QueueCorrupt):
        q.pending()


def _append(q, event):
    with q.path.open("a") as handle:
        handle.write(json.dumps({"schema": queue.SCHEMA, **event}) + "\n")


@pytest.mark.parametrize(
    "bad",
    [
        {"request_id": "zzz", "answer": 7},  # wrong id, no panel
        None,  # not even an object
    ],
)
def test_replay_refuses_a_hand_appended_answered_line_that_is_not_a_valid_response(
    q, bad
):
    r = req()
    sha = q.enqueue(r, OFFLINE)
    _append(q, {"event": "answered", "sha256": sha, "attempt": 1, "response": bad})
    for call in (q.status, q.counts, q.pending, q.request):
        with pytest.raises(queue.QueueCorrupt, match="not a valid response"):
            call(sha) if call in (q.status, q.request) else call()


def test_replay_refuses_an_answered_line_whose_panel_disagrees_without_an_adjudicator(
    q,
):
    r = req()
    sha = q.enqueue(r, OFFLINE)
    bad = json.loads(resp(r, a=True, b=False))  # disagree, no adjudicator
    _append(q, {"event": "answered", "sha256": sha, "attempt": 1, "response": bad})
    with pytest.raises(queue.QueueCorrupt, match="adjudication_missing"):
        q.status(sha)


def test_replay_still_accepts_a_valid_answered_line(q):
    r = req()
    sha = q.enqueue(r, OFFLINE)
    _append(
        q,
        {
            "event": "answered",
            "sha256": sha,
            "attempt": 1,
            "response": json.loads(resp(r)),
        },
    )
    st = q.status(sha)
    assert st.state == queue.ANSWERED and st.response is not None
    assert q.counts()["answered_external"] == 1


def test_a_live_plan_question_is_physical_and_never_enqueued(q):
    with pytest.raises(queue.PhysicalQuestion, match="goes to a person"):
        q.enqueue(req(question_kind="plan"), Context.LIVE)
    assert not q.path.exists()  # nothing written
    assert q.counts()["total"] == 0


def test_an_offline_completion_check_is_not_physical_and_is_accepted(q):
    sha = q.enqueue(req(question_kind="completion_check"), Context.OFFLINE)
    assert q.status(sha).state == queue.PENDING


@pytest.mark.parametrize(
    "kind, context",
    [
        ("no_such_kind", Context.OFFLINE),  # unregistered kind
        ("no_such_kind", Context.LIVE),
        ("boundary", Context.LIVE),  # kind exists, (kind, live) does not: unlisted
        ("completion_check", "elsewhere"),  # unknown context
        ("outcome_label", None),
    ],
)
def test_an_unregistered_kind_or_context_counts_as_physical(q, kind, context):
    with pytest.raises(queue.PhysicalQuestion):
        q.enqueue(req(question_kind=kind), context)
    assert not q.path.exists()


def test_every_physical_row_of_the_table_is_refused_and_every_other_is_accepted(
    tmp_path,
):
    from levi.decision import safety

    for n, ((kind, context), value) in enumerate(safety.SAFETY_TABLE.items()):
        qq = queue.Queue(tmp_path / f"q{n}.jsonl", DENY)
        request = req(rid=f"r{n:08d}", question_kind=kind)
        if value is safety.Safety.PHYSICAL:
            with pytest.raises(queue.PhysicalQuestion):
                qq.enqueue(request, context)
        else:
            qq.enqueue(request, context)
