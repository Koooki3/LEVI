"""Synthetic-only request evidence disclosure. No services or real run data."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from levi.automatic import api, subtask_view

RID = "synthetic:round1:s1"
EPISODE = "synthetic.forward.0001"
PRIVATE_MARKER = "private-report-marker<script>bad</script>"


def note(seq, code, detail):
    return SimpleNamespace(
        record="note",
        run_id="synthetic",
        sequence_no=seq,
        emitted_wall_ns=seq,
        note=SimpleNamespace(code=code, detail=json.dumps(detail)),
    )


def write(base, name, data):
    path = base / name
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    return {"file": name, "file_sha256": hashlib.sha256(raw).hexdigest()}


def fixture(base, kind="check-forward"):
    loop = kind.split("-")[-1]
    check = kind.startswith("check-")
    eid = EPISODE if loop == "forward" else "synthetic.reset.0001"
    context = {"kind": kind, "round_id": RID, "episode_id": eid if check else None}
    private = {
        "schema": "levi.aeri.request_evidence.v1",
        "evidence_context": context,
        "vlm_request_id": "bridge-id",
        "answer_content_sha256": "a" * 64,
        "prompt_text": PRIVATE_MARKER,
        "raw_content": PRIVATE_MARKER,
        "prompt_bytes": 42,
        "answer_bytes": 42,
        "raw_content_truncated": True,
        "external_evidence": {
            "schema": "levi.aeri.external_evidence.v1",
            "binding_verified": True,
            "report_source": "external_self_reported",
            "report_status": "reported",
            "bridge_wait_ms": 0,
            "report": {
                "schema": "levi.aeri.external_report.v1",
                "request_id": "bridge-id",
                "answer_sha256": "a" * 64,
                "agent_id": "agent",
                "agent_notes": PRIVATE_MARKER,
                "elapsed_ms": 9,
                "tokens": {
                    "input_new": 0,
                    "output": 3,
                    "cache_read": None,
                    "cache_write": 0,
                },
            },
        },
    }
    ref = write(base, "subtasks/000004-request-evidence.json", private)
    request = {
        "loop": loop,
        "episode_id": eid,
        "latency_ms": 12,
        "request_evidence": ref,
    }
    code = "subtask_check" if check else "subtask_plan"
    sidecar = write(
        base,
        "subtasks/request.json",
        request if check else {"request": request, "subtasks": []},
    )
    detail = {"round": 1, "loop": loop, "episode_id": eid, "index": 0, "attempt": 1}
    detail.update(
        sidecar
        if check
        else {"plan_file": sidecar["file"], "sha256": sidecar["file_sha256"]}
    )
    events = [
        note(1, "subtask_round_released", {"round": 1, "round_id": RID}),
        note(
            2, "subtask_started", {"round": 1, "loop": "forward", "episode_id": EPISODE}
        ),
        note(4, code, detail),
    ]
    if loop == "reverse":
        events.insert(
            2, note(3, "subtask_started", {"round": 1, "loop": loop, "episode_id": eid})
        )
    return events, private, eid


def label(base, value="success", reverse=(), record="label", rid=RID):
    path = base / "labels/round_label.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema": "levi.aeri.round_label.v1",
        "kind": "round_label",
        "record": record,
        "round_id": rid,
        "value": value,
        "by": "operator",
        "members": {"reverse": list(reverse)},
    }
    if record == "label":
        data["members"]["forward"] = [EPISODE]
    with path.open("a") as handle:
        handle.write(json.dumps(data) + "\n")


def view(base, events):
    return subtask_view.view(
        events,
        base,
        lambda parts: api._read_below(base, parts, api.MAX_JSON_BYTES),
        reset_mode="subtask_loop",
    )


@pytest.mark.parametrize(
    "kind", ["plan-forward", "check-forward", "plan-reverse", "check-reverse"]
)
@pytest.mark.parametrize("value", ["success", "failure"])
def test_exact_human_round_releases_only_matching_evidence(tmp_path, kind, value):
    events, _, eid = fixture(tmp_path, kind)
    before = view(tmp_path, events)
    assert PRIVATE_MARKER not in json.dumps(before)
    label(tmp_path, value, [eid] if kind.endswith("reverse") else [])
    report = view(tmp_path, events)["request_evidence"][0]
    assert report["visibility"] == "visible"
    assert report["context"]["round_id"] == RID
    assert report["report"]["tokens"] == {
        "input_new": 0,
        "output": 3,
        "cache_read": None,
        "cache_write": 0,
    }
    assert report["report"]["bridge_wait_ms"] == 0
    assert report["report"]["client_latency_ms"] == 12
    assert report["report"]["self_reported_elapsed_ms"] == 9
    assert report["report"]["raw_content_truncated"] is True
    assert "request-evidence.json" not in json.dumps(report)


def test_reverse_check_waits_for_members_and_latest_discard_revokes(tmp_path):
    events, _, eid = fixture(tmp_path, "check-reverse")
    label(tmp_path)
    assert PRIVATE_MARKER not in json.dumps(view(tmp_path, events))
    label(tmp_path, reverse=[eid], record="members")
    assert view(tmp_path, events)["request_evidence"][0]["visibility"] == "visible"
    label(tmp_path, "discarded")
    assert PRIVATE_MARKER not in json.dumps(view(tmp_path, events))


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "members_only",
        "discarded",
        "other_round",
        "torn",
        "bad_record",
        "oversized",
        "symlink",
    ],
)
def test_labels_fail_closed(tmp_path, problem):
    events, _, _ = fixture(tmp_path)
    if problem != "missing":
        label(
            tmp_path,
            "discarded" if problem == "discarded" else "success",
            record="members" if problem == "members_only" else "label",
            rid="other" if problem == "other_round" else RID,
        )
        path = tmp_path / "labels/round_label.jsonl"
        if problem == "torn":
            path.write_bytes(path.read_bytes() + b"{")
        elif problem == "bad_record":
            label(tmp_path, record="unknown")
        elif problem == "oversized":
            path.write_bytes(b" " * (api.MAX_JSON_BYTES + 1))
        elif problem == "symlink":
            other = tmp_path / "labels-copy"
            path.rename(other)
            path.symlink_to(other)
    assert PRIVATE_MARKER not in json.dumps(view(tmp_path, events))


@pytest.mark.parametrize(
    "problem",
    [
        "legacy",
        "inventory",
        "wrong_round",
        "wrong_episode",
        "wrong_kind",
        "second_release",
        "missing_started",
        "wrong_member",
        "digest",
        "symlink",
    ],
)
def test_bad_or_missing_context_cannot_disclose(tmp_path, problem):
    events, private, _ = fixture(tmp_path)
    label(tmp_path)
    if problem == "legacy":
        private.pop("evidence_context")
    elif problem == "inventory":
        private["evidence_context"] = {
            "kind": "inventory",
            "round_id": None,
            "episode_id": None,
        }
    elif problem == "wrong_round":
        private["evidence_context"]["round_id"] = "another:round1:s1"
    elif problem == "wrong_episode":
        private["evidence_context"]["episode_id"] = "other.forward.0001"
    elif problem == "wrong_kind":
        private["evidence_context"]["kind"] = "plan-forward"
    elif problem == "second_release":
        events.insert(
            2,
            note(
                3,
                "subtask_round_released",
                {"round": 1, "round_id": "synthetic:round1:s3"},
            ),
        )
    elif problem == "missing_started":
        del events[1]
    elif problem == "wrong_member":
        data = json.loads((tmp_path / "labels/round_label.jsonl").read_text())
        data["members"]["forward"] = ["other"]
        (tmp_path / "labels/round_label.jsonl").write_text(json.dumps(data) + "\n")
    ref = write(tmp_path, "subtasks/000004-request-evidence.json", private)
    request = {"loop": "forward", "episode_id": EPISODE, "request_evidence": ref}
    if problem == "digest":
        ref["file_sha256"] = "b" * 64
    if problem == "symlink":
        path = tmp_path / ref["file"]
        other = tmp_path / "private-copy"
        path.rename(other)
        path.symlink_to(other)
    sidecar = write(tmp_path, "subtasks/request.json", request)
    d = json.loads(events[-1].note.detail)
    d.update(sidecar)
    events[-1].note.detail = json.dumps(d)
    assert PRIVATE_MARKER not in json.dumps(view(tmp_path, events))


def test_full_journal_context_survives_display_truncation(tmp_path, monkeypatch):
    events, _, _ = fixture(tmp_path)
    label(tmp_path)
    monkeypatch.setattr(subtask_view, "MAX_NOTES", 1)
    result = view(tmp_path, events)
    assert result["truncated"] is True
    assert result["request_evidence"][0]["visibility"] == "visible"


def test_private_reference_not_expanded_or_returned_in_legacy_detail(tmp_path):
    ref = write(
        tmp_path,
        "subtasks/000001-request-evidence.json",
        {"agent_notes": PRIVATE_MARKER},
    )
    events = [note(1, "subtask_check", {"request_evidence": ref})]
    result = view(tmp_path, events)
    assert "request-evidence.json" not in json.dumps(result)
    assert PRIVATE_MARKER not in json.dumps(result)


@pytest.mark.parametrize("value", [True, -1, 1.5, "0", {}, []])
def test_invalid_token_is_unknown_not_zero(tmp_path, value):
    events, private, _ = fixture(tmp_path)
    private["external_evidence"]["report"]["tokens"]["input_new"] = value
    ref = write(tmp_path, "subtasks/000004-request-evidence.json", private)
    request = {"loop": "forward", "episode_id": EPISODE, "request_evidence": ref}
    sidecar = write(tmp_path, "subtasks/request.json", request)
    detail = json.loads(events[-1].note.detail)
    detail.update(sidecar)
    events[-1].note.detail = json.dumps(detail)
    label(tmp_path)
    assert (
        view(tmp_path, events)["request_evidence"][0]["report"]["tokens"]["input_new"]
        is None
    )


def repeated_questions(base, events, private, count):
    detail = json.loads(events[-1].note.detail)
    out = events[:2]
    for seq in range(4, 4 + count):
        ref = write(base, f"subtasks/{seq:06d}-request-evidence.json", private)
        sidecar = write(
            base,
            f"subtasks/{seq:06d}-request.json",
            {"loop": "forward", "episode_id": EPISODE, "request_evidence": ref},
        )
        out.append(note(seq, "subtask_check", {**detail, **sidecar}))
    return out


def test_new_projection_limits_questions_and_free_text(tmp_path):
    events, private, _ = fixture(tmp_path)
    private["prompt_text"] = "p" * 20000
    private["raw_content"] = "a" * 20000
    private["external_evidence"]["report"]["agent_notes"] = "n" * 4000
    events = repeated_questions(tmp_path, events, private, 20)
    label(tmp_path)
    result = view(tmp_path, events)
    assert len(result["request_evidence"]) == 16
    assert result["request_evidence_truncated"] is True
    reports = [r["report"] for r in result["request_evidence"] if r["report"]]
    assert (
        sum(
            len(r[key] or "")
            for r in reports
            for key in ("agent_notes", "prompt_text", "raw_content")
        )
        == 64000
    )
    assert any(r["display_text_truncated"] for r in reports)


def test_new_projection_read_budget_reserves_before_read(tmp_path):
    from levi.automatic import request_evidence_view

    events, private, _ = fixture(tmp_path)
    private["padding"] = "x" * 700000
    events = repeated_questions(tmp_path, events, private, 16)
    label(tmp_path)
    consumed = 0

    def read(parts):
        nonlocal consumed
        raw = api._read_below(tmp_path, parts, api.MAX_JSON_BYTES)
        consumed += len(raw)
        return raw

    rows = [
        {"seq": e.sequence_no, "code": e.note.code, "detail": json.loads(e.note.detail)}
        for e in events
    ]
    evidence, limited = request_evidence_view.project(
        events, rows, tmp_path, read, subtask_view._verified
    )
    assert limited is True
    assert consumed <= request_evidence_view.MAX_READ_BYTES
    assert 0 < sum(r["visibility"] == "visible" for r in evidence) < 16


def test_failed_reads_reserve_budget_and_stop_attempting_io(tmp_path):
    from levi.automatic import request_evidence_view

    events, private, _ = fixture(tmp_path)
    events = repeated_questions(tmp_path, events, private, 16)
    label(tmp_path)
    failures = 0

    def read(parts):
        nonlocal failures
        if parts[0] == "labels":
            return api._read_below(tmp_path, parts, api.MAX_JSON_BYTES)
        failures += 1
        raise OSError("synthetic failed read")

    rows = [
        {"seq": e.sequence_no, "code": e.note.code, "detail": json.loads(e.note.detail)}
        for e in events
    ]
    evidence, limited = request_evidence_view.project(
        events, rows, tmp_path, read, subtask_view._verified
    )
    assert limited is True
    assert failures == 3
    assert all(r["visibility"] == "hidden" for r in evidence)


def test_api_projection_is_read_only_and_does_not_offer_private_file_access(
    tmp_path, monkeypatch
):
    events, _, _ = fixture(tmp_path)
    run = SimpleNamespace(run_dir=tmp_path, events=events, reset_mode="subtask_loop")
    monkeypatch.setattr(api, "_locate", lambda run_id: run)
    before = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    hidden = api.run_subtasks("synthetic")
    assert PRIVATE_MARKER not in json.dumps(hidden)
    assert "request-evidence.json" not in json.dumps(hidden)
    assert {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    } == before
    # The generic evidence endpoint is fixed to evidence/, not private subtasks/.
    with pytest.raises(HTTPException) as exc:
        api.run_evidence("synthetic", "000004-request-evidence")
    assert exc.value.status_code == 404
    label(tmp_path)
    visible = api.run_subtasks("synthetic")
    assert visible["request_evidence"][0]["visibility"] == "visible"


def test_wrong_request_sequence_is_hidden_even_with_valid_digest(tmp_path):
    events, _, _ = fixture(tmp_path)
    label(tmp_path)
    events[-1].sequence_no = 5
    assert PRIVATE_MARKER not in json.dumps(view(tmp_path, events))


@pytest.mark.parametrize(
    "problem",
    [
        "release_run",
        "release_round",
        "release_sequence",
        "bool_round",
        "started_run",
        "started_round",
        "episode_run",
        "episode_role",
    ],
)
def test_bad_journal_association_fails_closed(tmp_path, problem):
    events, _, _ = fixture(tmp_path)
    label(tmp_path)
    target = (
        events[0]
        if problem.startswith("release") or problem == "bool_round"
        else events[1]
    )
    d = json.loads(target.note.detail)
    if problem in ("release_run", "started_run"):
        target.run_id = "another-run"
    elif problem in ("release_round", "started_round"):
        d["round"] = 2
    elif problem == "release_sequence":
        d["round_id"] = "synthetic:round1:s9"
    elif problem == "bool_round":
        d["round"] = True
    elif problem == "episode_run":
        d["episode_id"] = "another.forward.0001"
    elif problem == "episode_role":
        d["episode_id"] = "synthetic.reset.0001"
    target.note.detail = json.dumps(d)
    assert PRIVATE_MARKER not in json.dumps(view(tmp_path, events))


def test_production_fake_orchestrator_and_label_writers_bind_each_request(tmp_path):
    from subtask_rig import build

    from levi.automatic import metrics

    r = build(tmp_path)
    assert r.orch.run() == "COMPLETED"
    events = r.orch.journal.events
    rounds = metrics.rounds_of(events)
    ((rid, members),) = rounds.items()
    # Real _ask_vlm -> _sidecar -> _sub_note, using only the existing fake rig.
    rows, _ = subtask_view.notes_of(
        events,
        r.directory,
        lambda parts: api._read_below(r.directory, parts, api.MAX_JSON_BYTES),
        lambda sha: False,
    )
    checked = 0
    for row in rows:
        d = row["detail"] or {}
        if row["code"] not in ("subtask_plan", "subtask_check"):
            continue
        path = d.get("plan_file") if row["code"] == "subtask_plan" else d.get("file")
        request = json.loads((r.directory / path).read_text())
        if row["code"] == "subtask_plan":
            request = request["request"]
        name = request["request_evidence"]["file"]
        assert int(name.rsplit("/", 1)[1].split("-", 1)[0]) == row["seq"]
        checked += 1
    assert checked > 0
    assert all(
        row["visibility"] == "hidden"
        for row in view(r.directory, events)["request_evidence"]
    )
    metrics.label_round(
        r.directory, rid, "success", by="op-1", forward=members["forward"]
    )
    before = view(r.directory, events)["request_evidence"]
    assert any(row["visibility"] == "visible" for row in before)
    metrics.round_members(r.directory, rid, members["reverse"])
    after = view(r.directory, events)["request_evidence"]
    assert sum(row["visibility"] == "visible" for row in after) > sum(
        row["visibility"] == "visible" for row in before
    )
    metrics.label_round_after(r.directory, rid, "discarded", by="op-2")
    assert all(
        row["visibility"] == "hidden"
        for row in view(r.directory, events)["request_evidence"]
    )
