"""The automatic approver: explicit, scoped, stamped and audited -- and the
human path, and everything that keeps a person's review meaning something,
unchanged."""

# ruff: noqa: F811

import json

import pytest
from test_agent_economy import bench, with_video  # noqa: F401
from test_agent_ergonomics import GRASP

from levi.agent.capabilities import invoke
from levi.agent.schema import TaskContext
from levi.agent.security import Principal
from levi.live import auto


@pytest.fixture
def live_ws(tmp_path, monkeypatch):
    """A live workspace marker and the approver switched on."""
    live = tmp_path / "live-ws"
    live.mkdir()
    auto.write_marker(live)
    monkeypatch.setattr(auto, "live_dir", lambda: live)
    monkeypatch.setenv(auto.ENABLE_ENV, "1")
    return live


def audit(live):
    path = live / auto.AUDIT
    return (
        [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    )


def temporal(context, dataset):
    return TaskContext(
        **{
            **context.model_dump(),
            "provider": "external",
            "cameras": [with_video(dataset)],
            "allow_media_egress": True,
            "episodes": [0, 1],
            "workflow": {
                "kind": "temporal",
                "definitions": [GRASP],
                "require_human_pilot": False,
            },
        }
    )


def stage(wb, context, run_id):
    agent = Principal("conn", datasets=(context.repo_id,))
    seg = {
        "start": 0.0,
        "end": 1.9,
        "subtask": "other",
        "outcome": "unknown",
        "description": "x",
    }
    return invoke(
        wb,
        agent,
        "annotations.propose_segments",
        {"run_id": run_id, "segments": {"0": [seg]}},
    )


def test_the_approver_is_off_without_the_switch_and_the_marker(tmp_path, monkeypatch):
    monkeypatch.delenv(auto.ENABLE_ENV, raising=False)
    live = tmp_path / "ws"
    live.mkdir()
    monkeypatch.setattr(auto, "live_dir", lambda: live)
    with pytest.raises(PermissionError, match="off"):
        auto.principal()
    monkeypatch.setenv(auto.ENABLE_ENV, "1")
    with pytest.raises(PermissionError, match="off"):  # no live marker
        auto.principal()
    auto.write_marker(live)
    assert auto.principal().auto and auto.principal().id == "live-auto"


def test_a_principal_from_an_http_request_can_never_be_the_approver():
    from levi.agent.api import principal

    class Request:
        headers = {}  # noqa: RUF012

    assert principal(Request()).auto is False


def test_an_approval_by_the_approver_is_stamped_auto_and_audited(
    bench, dataset, live_ws
):
    wb, context = bench
    approver = auto.principal()
    run = invoke(wb, approver, "runs.plan", temporal(context, dataset).model_dump())
    assert run["principal"] == "live-auto"
    invoke(wb, approver, "plans.approve", {"run_id": run["id"], "revision": 1})
    assert wb.store.get("runs", run["id"])["plan"]["approval"]["actor"] == "live-auto"
    receipt = stage(wb, context, run["id"])
    change = wb.store.get("changes", receipt["id"])
    invoke(wb, approver, "changes.validate", {"changeset_id": change["id"]})
    invoke(
        wb,
        approver,
        "changes.approve",
        {"changeset_id": change["id"], "revision": change["revision"]},
    )
    change = wb.store.get("changes", change["id"])
    assert change["provenance"]["reviewer_type"] == "auto"
    assert change["provenance"]["reviewer"] == "live-auto"
    invoke(
        wb,
        approver,
        "changes.commit",
        {"changeset_id": change["id"], "revision": change["revision"]},
        key="live:test",
    )
    folder = wb.store.bundle(run["dataset_key"])
    atoms = json.loads((folder / "annotations/episode_000000.json").read_text())[
        "atoms"
    ]
    assert [a["levi"]["review"] for a in atoms] == ["auto"]
    assert atoms[0]["levi"]["origin"]["review"] == "auto"
    assert not (folder / "annotations/outcomes").exists()  # no human outcome label
    # Each state-changing call twice: allowed, then what it came to.
    assert [(x["tool"], x["decision"]) for x in audit(live_ws)] == [
        ("runs.plan", "allowed"),
        ("runs.plan", "completed"),
        ("plans.approve", "allowed"),
        ("plans.approve", "completed"),
        ("changes.approve", "allowed"),
        ("changes.approve", "completed"),
        ("changes.commit", "allowed"),
        ("changes.commit", "completed"),
    ]


def test_a_person_s_approval_is_unchanged_human_and_unmarked(bench, dataset):
    wb, context = bench
    human = Principal("reviewer", human=True)
    run = invoke(wb, human, "runs.plan", temporal(context, dataset).model_dump())
    invoke(wb, human, "plans.approve", {"run_id": run["id"], "revision": 1})
    change = wb.store.get("changes", stage(wb, context, run["id"])["id"])
    invoke(
        wb,
        human,
        "changes.approve",
        {"changeset_id": change["id"], "revision": change["revision"]},
    )
    change = wb.store.get("changes", change["id"])
    assert change["provenance"]["reviewer_type"] == "human"
    invoke(
        wb,
        human,
        "changes.commit",
        {"changeset_id": change["id"], "revision": change["revision"]},
        key="human:test",
    )
    atoms = json.loads(
        (
            wb.store.bundle(run["dataset_key"]) / "annotations/episode_000000.json"
        ).read_text()
    )["atoms"]
    assert (
        "review" not in atoms[0]["levi"] and "review" not in atoms[0]["levi"]["origin"]
    )


def test_an_agent_cannot_approve_or_commit_as_before(bench, dataset):
    wb, context = bench
    human = Principal("reviewer", human=True)
    run = invoke(wb, human, "runs.plan", temporal(context, dataset).model_dump())
    invoke(wb, human, "plans.approve", {"run_id": run["id"], "revision": 1})
    change = wb.store.get("changes", stage(wb, context, run["id"])["id"])
    agent = Principal("conn", datasets=(context.repo_id,))
    for name in ("changes.approve", "changes.commit"):
        with pytest.raises(PermissionError):
            invoke(
                wb,
                agent,
                name,
                {"changeset_id": change["id"], "revision": change["revision"]},
                key="k",
            )


def test_the_approver_leaves_a_person_s_runs_alone_and_refuses_everything_else(
    bench, dataset, live_ws
):
    wb, context = bench
    approver = auto.principal()
    human = Principal("reviewer", human=True)
    mine = invoke(wb, human, "runs.plan", temporal(context, dataset).model_dump())
    with pytest.raises(PermissionError, match="planned itself"):
        invoke(wb, approver, "plans.approve", {"run_id": mine["id"], "revision": 1})
    with pytest.raises(PermissionError, match="may not call"):
        invoke(wb, approver, "workspace.reset", {"apply": False})
    with pytest.raises(PermissionError, match="may not call"):
        invoke(
            wb,
            approver,
            "plans.review_pilot",
            {"run_id": mine["id"], "revision": 1, "accepted": True},
        )
    decisions = [(x["tool"], x["decision"]) for x in audit(live_ws)]
    assert ("plans.approve", "refused") in decisions
    assert ("workspace.reset", "refused") in decisions
    # Without the switch the same principal object is refused outright.
    import os

    os.environ.pop(auto.ENABLE_ENV)
    with pytest.raises(PermissionError, match="off"):
        invoke(wb, approver, "runs.get", {"run_id": mine["id"]})


def test_the_atom_mark_survives_a_plain_save_and_turns_to_edited_when_a_person_changes_it(
    client, dataset
):
    from levi import catalog

    entry = catalog.register(str(dataset))
    repo = entry["id"]
    written = ["segment text", 0.0, 1.0]
    atom = {
        "role": "assistant",
        "content": "segment text",
        "style": "subtask",
        "timestamp": 0.0,
        "to": 1.0,
        "levi": {"review": "auto", "origin": {"kind": "agent", "written": written}},
    }

    def save(value):
        response = client.post(
            "/annotations/api/episodes/0/atoms",
            json={"repo_id": repo, "episode_index": 0, "atoms": [value]},
        )
        assert response.status_code == 200, response.text
        saved = client.get(f"/annotations/api/episodes/0/atoms?repo_id={repo}").json()
        return saved["atoms"][0]["levi"]["review"]

    assert save(atom) == "auto"  # saving the page is not a review
    assert save({**atom, "content": "my wording"}) == "edited"
    assert save({**atom, "to": 1.5}) == "edited"


def test_the_approver_cannot_approve_or_commit_a_review_or_an_outcome(
    bench, dataset, live_ws
):
    """D5 is enforced by the authorization layer, not by the worker's manners:
    an anchored review proposes an outcome, and committing one writes a human
    outcome label."""
    wb, context = bench
    approver = auto.principal()
    plan = TaskContext(
        **{
            **context.model_dump(),
            "provider": "external",
            "workflow": {"kind": "review", "require_human_pilot": False},
        }
    )
    run = invoke(wb, approver, "runs.plan", plan.model_dump())
    outcome = {
        "episode_index": 0,
        "kind": "outcome",
        "content": "x",
        "start": 0.0,
        "outcome": "success",
        "evidence_ids": ["e"],
    }
    wb.store.put(
        "changes",
        "c-review",
        {
            "id": "c-review",
            "run_id": run["id"],
            "status": "draft",
            "revision": 0,
            "proposals": [outcome],
            "provenance": {},
        },
    )
    for name in ("changes.approve", "changes.commit"):
        with pytest.raises(PermissionError, match="never a review"):
            invoke(
                wb,
                approver,
                name,
                {"changeset_id": "c-review", "revision": 0},
                key="k",
            )
    # A temporal run with an outcome proposal smuggled in is refused too.
    temporal_run = invoke(
        wb, approver, "runs.plan", temporal(context, dataset).model_dump()
    )
    wb.store.put(
        "changes",
        "c-mixed",
        {
            "id": "c-mixed",
            "run_id": temporal_run["id"],
            "status": "draft",
            "revision": 0,
            "proposals": [outcome],
            "provenance": {},
        },
    )
    with pytest.raises(PermissionError, match="outcome"):
        invoke(
            wb, approver, "changes.approve", {"changeset_id": "c-mixed", "revision": 0}
        )
    refused = [x for x in audit(live_ws) if x["decision"] == "refused"]
    assert [x["tool"] for x in refused] == [
        "changes.approve",
        "changes.commit",
        "changes.approve",
    ]
    assert not list(live_ws.rglob("outcomes"))


def test_writing_an_outcome_label_refuses_anything_the_approver_approved(tmp_path):
    from levi.agent.formats import ANNOTATIONS

    proposal = {"episode_index": 0, "outcome": "success", "kind": "outcome"}
    with pytest.raises(ValueError, match="cannot write an outcome label"):
        ANNOTATIONS["outcome"].apply(
            proposal,
            app=None,
            state=None,
            atoms=[],
            folder=tmp_path,
            origin={"review": "auto"},
        )


def test_a_failed_call_is_audited_with_its_error(bench, dataset, live_ws):
    wb, context = bench
    approver = auto.principal()
    run = invoke(wb, approver, "runs.plan", temporal(context, dataset).model_dump())
    with pytest.raises(Exception, match="revision"):
        invoke(wb, approver, "plans.approve", {"run_id": run["id"], "revision": 99})
    last = audit(live_ws)[-1]
    assert (last["tool"], last["decision"]) == ("plans.approve", "failed")
    assert "revision" in last["error"].lower()
    # Pure reads are not logged.
    before = len(audit(live_ws))
    invoke(wb, approver, "runs.get", {"run_id": run["id"]})
    assert len(audit(live_ws)) == before


# --- a person cannot start the model while the policy infers -------------------------------


def write_gate(live, open_, age=0.0, idle=False):
    import time

    (live / "gate.json").write_text(
        json.dumps(
            {
                "open": open_,
                "code": "ok" if open_ else "policy_inferring",
                "reason": "" if open_ else "the policy is inferring",
                "updated_at": time.time() - age,
                "idle": idle,
            }
        )
    )


def test_a_person_s_execute_is_refused_while_the_gate_is_closed(
    bench, dataset, live_ws
):
    from levi.agent.store import Conflict
    from levi.live import gating

    wb, context = bench
    human = Principal("reviewer", human=True)
    run = invoke(wb, human, "runs.plan", temporal(context, dataset).model_dump())
    invoke(wb, human, "plans.approve", {"run_id": run["id"], "revision": 1})
    write_gate(live_ws, False)
    for name in ("runs.execute", "runs.resume"):
        with pytest.raises(Conflict, match="inferring, try again shortly"):
            invoke(wb, human, name, {"run_id": run["id"]})
    # Only those two, only people: reads and the worker's own principals pass.
    for who, name in (
        (human, "runs.get"),
        (human, "plans.approve"),
        (Principal("live-planner", human=True), "runs.execute"),
        (Principal("live-auto", human=True, auto=True), "runs.resume"),
    ):
        gating.check(who, name)
    # A gate nobody refreshes (the supervisor died) is read the careful way,
    # the same for a person and for the worker: closed, unless its last word
    # was "nothing to protect" (no policy server, no evaluation). Open gates
    # and workspaces that are not live ones block nothing.
    write_gate(live_ws, False, age=gating.STALE_S + 5)
    with pytest.raises(Conflict, match="supervisor"):
        gating.check(human, "runs.execute")
    write_gate(live_ws, True, age=gating.STALE_S + 5)  # open, but a policy was up
    with pytest.raises(Conflict, match="supervisor"):
        gating.check(human, "runs.execute")
    write_gate(live_ws, True, age=gating.STALE_S + 5, idle=True)
    gating.check(human, "runs.execute")
    write_gate(live_ws, True)
    gating.check(human, "runs.execute")
    write_gate(live_ws, False)
    (live_ws / auto.MARKER).unlink()
    gating.check(human, "runs.execute")


def test_a_failing_audit_write_is_a_warning_not_a_failed_call(
    bench, dataset, live_ws, monkeypatch, capsys
):
    wb, context = bench
    approver = auto.principal()
    run = invoke(wb, approver, "runs.plan", temporal(context, dataset).model_dump())

    def broken(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(auto.jsonio, "append_line", broken)
    before = auto.AUDIT_FAILURES
    # The call succeeds (it already took effect) ...
    approved = invoke(
        wb, approver, "plans.approve", {"run_id": run["id"], "revision": 1}
    )
    assert approved
    assert wb.store.get("runs", run["id"])["plan"].get("approval")
    # ... the failures are counted (allowed + completed) and said once, loudly.
    assert auto.AUDIT_FAILURES == before + 2
    assert "audit" in capsys.readouterr().err.lower()
    # A refusal is still a refusal, not an OSError.
    with pytest.raises(PermissionError, match="may not call"):
        invoke(wb, approver, "workspace.reset", {"apply": False})
    assert auto.AUDIT_FAILURES >= before + 3


# --- every model request obeys the gate, whoever started the run --------------------------


def test_every_request_reads_the_gate_and_a_run_stops_when_it_closes(
    live_ws, monkeypatch
):
    import time

    from levi.inference import gpu
    from levi.live import gating

    monkeypatch.delenv(gating.WORKER_ENV, raising=False)
    monkeypatch.setenv("LEVI_GPU_SHARING", "allow")  # the live core's setting
    write_gate(live_ws, True)
    assert gpu.require_free(None) is None  # started while the gate was open ...
    write_gate(live_ws, False)
    time.sleep(gating.CACHE_S + 0.05)  # (the read is cached for a moment)
    # ... and the next request is not sent: a retryable error, which makes the
    # run stop at that point (blocked, resumable by Resume once the gate opens).
    with pytest.raises(gpu.GpuBusy, match="inferring"):
        gpu.require_free(None)
    assert issubclass(gpu.GpuBusy, ValueError)  # what the executor turns into 'blocked'
    write_gate(live_ws, True)
    time.sleep(gating.CACHE_S + 0.05)
    assert gpu.require_free(None) is None
    # The read is cheap: within the cache time the file is not read again.
    write_gate(live_ws, False)
    assert gating.request_blocked() is None


def test_the_worker_and_other_workspaces_are_not_held_by_the_request_check(
    live_ws, monkeypatch, tmp_path
):
    import time

    from levi.live import gating

    write_gate(live_ws, False)
    time.sleep(gating.CACHE_S + 0.05)
    monkeypatch.delenv(gating.WORKER_ENV, raising=False)
    assert gating.request_blocked() is not None
    monkeypatch.setenv(gating.WORKER_ENV, "1")  # the worker obeys by standing down
    assert gating.request_blocked() is None
    monkeypatch.delenv(gating.WORKER_ENV)
    # A gate nobody refreshes (supervisor dead): careful, as for the worker,
    # unless its last word was "nothing to protect".
    write_gate(live_ws, False, age=gating.STALE_S + 5)
    time.sleep(gating.CACHE_S + 0.05)
    assert gating.request_blocked() is not None
    write_gate(live_ws, False, age=gating.STALE_S + 5, idle=True)
    time.sleep(gating.CACHE_S + 0.05)
    assert gating.request_blocked() is None
    # Not a live workspace: no marker, no check.
    write_gate(live_ws, False)
    (live_ws / auto.MARKER).unlink()
    time.sleep(gating.CACHE_S + 0.05)
    assert gating.request_blocked() is None


def test_the_task_console_s_advance_is_guarded_like_run_and_resume(live_ws):
    from levi.agent.store import Conflict
    from levi.live import gating

    write_gate(live_ws, False)
    with pytest.raises(Conflict, match="inferring"):
        gating.check(Principal("reviewer", human=True), "tasks.advance")


def test_a_stale_gate_is_read_strictly_by_the_worker_and_checked_for_people(
    live_ws, monkeypatch
):
    """A supervisor that died while idle leaves ``idle: true`` behind. The
    worker (a separate session that nobody supervises any more) never takes
    that as open: it would keep sending requests when a policy server appears
    later. A person is let through only if no policy port listens right now."""
    import time

    from levi.live import gating, gpumgr

    now = time.time()
    assert gating.closed({"open": False, "updated_at": now})
    assert not gating.closed({"open": True, "updated_at": now})
    old = now - gating.STALE_S - 1
    stale_idle = {"open": True, "updated_at": old, "idle": True, "policy_ports": [8000]}
    stale_busy = {"open": True, "updated_at": old, "idle": False}
    monkeypatch.setattr(gpumgr, "listening_ports", lambda: set())
    # People: idle and no policy server listening -> nothing to protect.
    assert not gating.closed(stale_idle)
    assert gating.closed(stale_busy) and gating.closed({**stale_busy, "open": False})
    # ... but one has appeared since the supervisor died: closed.
    monkeypatch.setattr(gpumgr, "listening_ports", lambda: {8000})
    assert gating.closed(stale_idle)
    # The worker: stale is closed, whatever it said.
    monkeypatch.setattr(gpumgr, "listening_ports", lambda: set())
    assert gating.closed(stale_idle, worker=True)
    assert gating.closed(stale_busy, worker=True)
    assert not gating.closed({"open": True, "updated_at": now}, worker=True)
    assert not gating.closed(None) and not gating.closed("junk")
