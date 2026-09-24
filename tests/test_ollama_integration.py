"""Existing Harness + native Ollama using protocol fakes, never a real service."""

import builtins
import json
import threading
import time

import httpx
import pytest

from levi.agent.schema import Budget, ProviderConfig, TaskContext
from levi.agent.store import Conflict, Store, file_hash
from levi.inference import models, provider
from levi.inference.ollama import OllamaClient, OllamaError
from levi.inference.transport import OllamaTransport, validate_ollama_endpoint

DIGEST = "a" * 64


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.installed = True
        self.digest = DIGEST
        self.release = threading.Event()
        self.release.set()
        self.download_started = threading.Event()

    def request(self, method, path, payload):
        self.calls.append((method, path, payload))
        if path == "/api/tags":
            return {
                "models": [{"name": "qwen3.5:4b", "digest": self.digest, "size": 10}]
                if self.installed
                else []
            }
        if path == "/api/version":
            return {"version": "test-only"}
        if path == "/api/show":
            return {"capabilities": ["vision", "completion"], "template": "fixture"}
        if path == "/api/chat":
            observation = json.loads(payload["messages"][-1]["content"])
            summary, evidence = observation["summary"], observation["evidence"]
            return {
                "done": True,
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "Fixture inspection",
                            "proposals": [
                                {
                                    "kind": "segment",
                                    "episode_index": summary["episode_index"],
                                    "content": "Move gripper",
                                    "start": summary["start"],
                                    "end": summary["end"],
                                    "evidence_ids": [evidence[0]["id"]],
                                }
                            ],
                        }
                    )
                },
                "prompt_eval_count": 40,
                "eval_count": 20,
            }
        raise AssertionError(path)

    def stream(self, method, path, payload):
        assert path == "/api/pull"
        self.download_started.set()
        assert self.release.wait(5), "Test did not release mocked download"
        yield {"status": "pulling", "completed": 5, "total": 10}
        self.installed = True
        yield {"status": "success"}


@pytest.fixture(autouse=True)
def guard(monkeypatch):
    original = builtins.__import__

    def forbidden(name, *args, **kwargs):
        if name.split(".")[0] in {
            "torch",
            "jax",
            "sam3",
            "tensorflow",
            "pydantic_ai",
            "openai",
        }:
            raise AssertionError("Real model and accelerator imports are forbidden")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbidden)

    def network_forbidden(*args, **kwargs):
        raise AssertionError("A test attempted a real model connection")

    monkeypatch.setattr(OllamaTransport, "_client", network_forbidden)


@pytest.fixture
def native(monkeypatch):
    transport = FakeTransport()
    factory = lambda *args, **kwargs: OllamaClient(transport)
    monkeypatch.setattr(models, "client_for", factory)
    monkeypatch.setattr(provider, "client_for", factory)
    return transport


def config(**kwargs):
    return ProviderConfig(
        **(
            {
                "name": "ollama",
                "kind": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "model": "qwen3.5:4b",
                "allow_localhost": True,
                "structured_output": True,
            }
            | kwargs
        )
    )


def test_configure_inspect_bind_and_no_credentials(client, native, monkeypatch):
    monkeypatch.setenv("LEVI_MODEL_API_KEY", "must-not-be-used")
    assert (
        client.post(
            "/api/levi/agent/v1/providers", json=config().model_dump()
        ).status_code
        == 200
    )
    result = client.get("/api/levi/agent/v1/providers").json()[0]
    assert result["credential_source"] == "not_required"
    status = client.get("/api/levi/agent/v1/providers/ollama/ollama").json()
    assert status["model"]["digest"] == DIGEST
    assert not status["quality_verified"]
    assert all(row[1] != "/api/chat" for row in native.calls)
    bound = client.post(
        "/api/levi/agent/v1/providers/ollama/ollama/bind",
        json={"digest": DIGEST, "vision": True, "structured_output": True},
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["model_digest"] == DIGEST
    assert (
        client.post(
            "/api/levi/agent/v1/providers/ollama/session", json={"key": "unused"}
        ).status_code
        == 422
    )
    from levi.agent.credentials import get

    assert get(config()) is None


def test_shared_harness_mock_pilot_keeps_raw_source_unchanged(client, dataset, native):
    from levi import catalog, service
    from levi.agent.planning import approve
    from levi.agent.runtime import Workbench

    entry = catalog.register(str(dataset))
    source = {
        str(path): file_hash(path) for path in dataset.rglob("*") if path.is_file()
    }
    wb = Workbench(service.STATE)
    wb.store.put("providers", "ollama", config(model_digest=DIGEST).model_dump())
    run = wb.plan(
        TaskContext(
            repo_id=entry["id"],
            episodes=[0],
            instruction="Review motion",
            provider="ollama",
        )
    )
    approve(wb, run["id"], 1, "human")
    assert wb.store.claim(run["id"], "test-owner")
    wb.execute(run["id"], "test-owner", pilot=True)
    result = wb.store.get("runs", run["id"])
    assert result["status"] == "waiting_for_review", result
    assert result["completed"] == [0]
    assert wb.store.get("changes", result["changes"])["status"] == "draft"
    assert any(row[1] == "/api/chat" for row in native.calls)
    assert source == {
        str(path): file_hash(path) for path in dataset.rglob("*") if path.is_file()
    }


def test_missing_digest_prevents_plan(client, dataset, native):
    from levi import catalog, service
    from levi.agent.runtime import Workbench

    entry = catalog.register(str(dataset))
    wb = Workbench(service.STATE)
    wb.store.put("providers", "ollama", config().model_dump())
    with pytest.raises(ValueError, match="digest"):
        wb.plan(
            TaskContext(
                repo_id=entry["id"],
                episodes=[0],
                instruction="review",
                provider="ollama",
            )
        )
    assert native.calls == []


def terminal(store, job_id):
    for _ in range(100):
        result = models.download(store, job_id)
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(0.01)
    raise AssertionError("Mock download did not finish")


def test_download_idempotency_cancellation_and_readable_unique_ids(tmp_path, native):
    store = Store(tmp_path)
    store.put("providers", "ollama", config().model_dump())
    native.release.clear()
    first = models.start_download(store, "ollama", "request-1")
    try:
        assert native.download_started.wait(2)
        assert models.start_download(store, "ollama", "request-1")["id"] == first["id"]
        with pytest.raises(Conflict, match="active"):
            models.start_download(store, "ollama", "request-2")
        assert models.cancel(store, first["id"])["cancel_requested"]
    finally:
        native.release.set()
    assert terminal(store, first["id"])["status"] == "cancelled"
    second = models.start_download(store, "ollama", "request-2")
    assert second["id"] != first["id"]
    assert terminal(store, second["id"])["status"] == "succeeded"
    assert "config" not in models.download(store, second["id"])


def test_download_control_rejects_agent_self_approval(client, monkeypatch):
    from levi.agent.security import Principal
    from levi.inference import api

    monkeypatch.setattr(
        api, "principal", lambda request: Principal("agent", operations=("configure",))
    )
    response = client.post(
        "/api/levi/agent/v1/providers/ollama/ollama/download",
        json={"request_id": "x", "approve_download": True},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("http://127.0.0.1:11434/v1", True),
        ("http://127.0.0.1:11434", False),
        ("http://user:pass@127.0.0.1:11434", True),
    ],
)
def test_endpoint_policy_rejects_ambiguous_endpoints(url, allowed):
    with pytest.raises(ValueError):
        validate_ollama_endpoint(url, allowed)


def test_http_redirect_is_not_followed(monkeypatch):
    transport = OllamaTransport(config())
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://unapproved.invalid"})

    monkeypatch.setattr(
        transport,
        "_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ),
    )
    with pytest.raises(OllamaError, match="302"):
        transport.request("GET", "/api/tags", None)
    assert len(calls) == 1


def test_response_bound_and_malformed_progress(monkeypatch):
    transport = OllamaTransport(config(), max_response_bytes=10)
    monkeypatch.setattr(
        transport,
        "_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=b"x" * 11)
            )
        ),
    )
    with pytest.raises(OllamaError, match="limit"):
        transport.request("GET", "/api/tags", None)


def test_out_of_scope_evidence_rejected_before_call(tmp_path, native):
    with pytest.raises(ValueError, match="outside"):
        provider.OllamaProvider().generate(
            config(model_digest=DIGEST, vision=True),
            "x",
            {},
            [{"artifact": "../secret"}],
            tmp_path,
            Budget(),
        )
    assert native.calls == []


def supervised_bench(client, dataset):
    from levi import catalog, service
    from levi.agent import grants
    from levi.agent.pilot_contracts import GrantRequest
    from levi.agent.runtime import Workbench

    entry = catalog.register(str(dataset))
    wb = Workbench(service.STATE)
    teacher = grants.create(
        wb.store, GrantRequest(client="codex", datasets=[entry["id"]])
    )
    principal = grants.authenticate(wb.store, teacher["token"])
    wb.store.put("providers", "ollama", config(model_digest=DIGEST).model_dump())
    context = TaskContext(
        repo_id=entry["id"],
        episodes=[0],
        instruction="Review motion",
        provider="ollama",
        supervision="supervised",
        teacher_grant=teacher["id"],
        allow_media_egress=True,
        budget=Budget(max_calls=1),
    )
    return wb, context, principal


def execute_supervised(wb, run_id):
    assert wb.store.claim(run_id, "test-teacher-run")
    wb.execute(run_id, "test-teacher-run", pilot=True)
    return wb.store.get("runs", run_id)


def test_teacher_gate_resume_reuses_settled_result_and_cannot_self_approve(
    client, dataset, native
):
    from levi.agent.capabilities import invoke
    from levi.agent.planning import approve
    from levi.agent.security import Principal

    wb, context, teacher = supervised_bench(client, dataset)
    run = wb.plan(context)
    approve(wb, run["id"], 1, "human")
    waiting = execute_supervised(wb, run["id"])
    assert waiting["status"] == "blocked"
    assert waiting["completed"] == []
    assert waiting["prepared"] == [0]
    assert waiting["changes"] is None
    phase = invoke(wb, teacher, "supervision.pending", {"run_id": run["id"]})["items"][
        0
    ]
    other = Principal(
        "unassigned", datasets=(context.repo_id,), operations=("read", "draft")
    )
    with pytest.raises(PermissionError, match="assigned teacher"):
        invoke(wb, other, "supervision.pending", {"run_id": run["id"]})
    with pytest.raises(PermissionError):
        invoke(wb, teacher, "plans.approve", {"run_id": run["id"], "revision": 1})
    with pytest.raises(PermissionError, match="only submit teacher feedback"):
        invoke(
            wb,
            teacher,
            "annotations.propose_segments",
            {"run_id": run["id"], "proposals": [], "inspected_episodes": [0]},
        )
    payload = {
        "run_id": run["id"],
        "teaching_id": phase["id"],
        "revision": 0,
        "decision": "accept",
        "note": "Evidence checked",
    }
    accepted = invoke(wb, teacher, "supervision.feedback", payload)
    assert invoke(wb, teacher, "supervision.feedback", payload) == accepted
    requests = len([row for row in native.calls if row[1] == "/api/chat"])
    resumed = execute_supervised(wb, run["id"])
    assert resumed["status"] == "waiting_for_review", resumed
    assert resumed["requests"] == waiting["requests"] == 1
    assert len([row for row in native.calls if row[1] == "/api/chat"]) == requests
    assert resumed["cache_hits"] == 1
    change = wb.store.get("changes", resumed["changes"])
    assert change["status"] == "draft"
    assert change["provenance"]["teacher_grant"] == teacher.id


def test_teacher_correction_scope_and_disconnect_fail_closed(client, dataset, native):
    from levi.agent.capabilities import invoke
    from levi.agent.planning import approve

    wb, context, teacher = supervised_bench(client, dataset)
    run = wb.plan(context)
    approve(wb, run["id"], 1, "human")
    execute_supervised(wb, run["id"])
    phase = invoke(wb, teacher, "supervision.pending", {"run_id": run["id"]})["items"][
        0
    ]
    wrong = phase["learner_output"]
    wrong["proposals"][0]["episode_index"] = 999
    with pytest.raises(ValueError):
        invoke(
            wb,
            teacher,
            "supervision.feedback",
            {
                "run_id": run["id"],
                "teaching_id": phase["id"],
                "revision": 0,
                "decision": "revise",
                "note": "bad scope",
                "output": wrong,
            },
        )
    assert wb.store.get("teaching", phase["id"])["status"] == "pending"
    before = len(native.calls)
    wb.store.mutate("grants", teacher.id, lambda value: value.update(enabled=False))
    blocked = execute_supervised(wb, run["id"])
    assert blocked["status"] == "blocked"
    assert "disconnected" in blocked["reason"]
    assert len(native.calls) == before


def test_memory_requests_require_bound_model_and_explicit_human_consent(
    client, native, monkeypatch
):
    assert (
        client.post(
            "/api/levi/agent/v1/providers", json=config().model_dump()
        ).status_code
        == 200
    )
    endpoint = "/api/levi/agent/v1/providers/ollama/ollama/memory"
    assert (
        client.post(
            endpoint, json={"operation": "load", "approve_hardware_use": True}
        ).status_code
        == 409
    )
    assert not any(call[1] == "/api/generate" for call in native.calls)
    assert (
        client.post(
            "/api/levi/agent/v1/providers",
            json=config(model_digest=DIGEST).model_dump(),
        ).status_code
        == 200
    )
    original = native.request
    requests = []

    def fake_request(method, path, payload):
        if path == "/api/generate":
            requests.append(payload)
            return {"done": True}
        return original(method, path, payload)

    monkeypatch.setattr(native, "request", fake_request)
    assert client.post(endpoint, json={"operation": "load"}).status_code == 422
    assert (
        client.post(
            endpoint, json={"operation": "load", "approve_hardware_use": True}
        ).status_code
        == 200
    )
    assert (
        client.post(
            endpoint, json={"operation": "unload", "approve_hardware_use": True}
        ).status_code
        == 200
    )
    assert [r["keep_alive"] for r in requests] == ["5m", 0]


def test_an_invalid_learner_answer_is_settled_and_handed_to_the_teacher(
    client, dataset, native, monkeypatch
):
    from levi.agent.capabilities import invoke
    from levi.agent.planning import approve
    from levi.agent.runtime import Workbench

    real = Workbench.validate_proposals
    calls = []

    def first_fails(context, proposals, evidence, summary):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError(
                "Temporal attempts require success/failure/unknown outcome"
            )
        return real(context, proposals, evidence, summary)

    monkeypatch.setattr(Workbench, "validate_proposals", staticmethod(first_fails))
    wb, context, teacher = supervised_bench(client, dataset)
    run = wb.plan(context)
    approve(wb, run["id"], 1, "human")
    waiting = execute_supervised(wb, run["id"])
    # The tokens were spent: they are on the run, not lost with the answer.
    assert waiting["status"] == "blocked" and waiting["tokens"] > 0
    phase = invoke(wb, teacher, "supervision.pending", {"run_id": run["id"]})["items"][
        0
    ]
    assert "outcome" in phase["learner_error"]
    payload = {
        "run_id": run["id"],
        "teaching_id": phase["id"],
        "revision": 0,
        "note": "Every segment states its outcome.",
    }
    with pytest.raises(ValueError, match="revise or reject"):
        invoke(wb, teacher, "supervision.feedback", {**payload, "decision": "accept"})
    invoke(
        wb,
        teacher,
        "supervision.feedback",
        {**payload, "decision": "revise", "output": phase["learner_output"]},
    )
    resumed = execute_supervised(wb, run["id"])
    assert resumed["status"] == "waiting_for_review", resumed
    assert resumed["cache_hits"] == 1


def test_an_unparseable_learner_answer_reaches_the_teacher_raw(
    client, dataset, native, monkeypatch
):
    from levi.agent.capabilities import invoke
    from levi.agent.planning import approve
    from levi.inference.provider import InvalidAnswer, OllamaProvider

    def unparseable(self, *a, **k):
        raise InvalidAnswer(
            "Model output failed validation: proposals.0: no end",
            '{"proposals": [{"start": 1}]}',
            {"requests": 1, "tokens": 77, "reported_tokens": 77},
        )

    monkeypatch.setattr(OllamaProvider, "generate", unparseable)
    wb, context, teacher = supervised_bench(client, dataset)
    run = wb.plan(context)
    approve(wb, run["id"], 1, "human")
    waiting = execute_supervised(wb, run["id"])
    assert waiting["tokens"] == 77
    phase = invoke(wb, teacher, "supervision.pending", {"run_id": run["id"]})["items"][
        0
    ]
    assert phase["learner_raw"].startswith('{"proposals"')
    assert "no end" in phase["learner_error"]


def test_a_failed_or_overspent_call_leaves_honest_accounts(
    client, dataset, native, monkeypatch
):
    from levi.agent.planning import approve
    from levi.inference.ollama import OllamaError
    from levi.inference.provider import OllamaProvider

    def refused(self, *a, **k):
        raise OllamaError("Ollama HTTP request failed (400)")

    monkeypatch.setattr(OllamaProvider, "generate", refused)
    wb, context, _ = supervised_bench(client, dataset)
    run = wb.plan(context)
    approve(wb, run["id"], 1, "human")
    failed = execute_supervised(wb, run["id"])
    assert failed["reason"].startswith("Ollama HTTP request failed")
    assert failed["reserved_tokens"] == 0 and failed["requests"] == 1

    def greedy(self, config, goal, summary, evidence, artifacts, budget):
        return {"summary": "x"}, {"requests": 1, "tokens": budget.max_tokens + 5}

    monkeypatch.setattr(OllamaProvider, "generate", greedy)
    wb.store.mutate("runs", run["id"], lambda r: r.update(requests=0))
    over = execute_supervised(wb, run["id"])
    assert "exceeded reserved budget" in over["reason"]
    assert over["reserved_tokens"] == 0 and over["tokens"] > 0


def test_a_reviewed_phase_whose_evidence_changed_is_asked_again(
    client, dataset, native
):
    from levi.agent.capabilities import invoke
    from levi.agent.planning import approve

    wb, context, teacher = supervised_bench(client, dataset)
    run = wb.plan(context)
    approve(wb, run["id"], 1, "human")
    execute_supervised(wb, run["id"])
    phase = invoke(wb, teacher, "supervision.pending", {"run_id": run["id"]})["items"][
        0
    ]
    # The harness changed what this phase shows after the teacher saw it.
    wb.store.mutate("teaching", phase["id"], lambda r: r.update(fingerprint="old"))
    execute_supervised(wb, run["id"])
    items = invoke(
        wb,
        teacher,
        "supervision.pending",
        {"run_id": run["id"], "include_decided": True},
    )["items"]
    statuses = sorted(item["status"] for item in items)
    assert statuses == ["pending", "superseded"]
    # What the teacher polls holds only what waits for it.
    waiting = invoke(wb, teacher, "supervision.pending", {"run_id": run["id"]})
    assert [item["status"] for item in waiting["items"]] == ["pending"]


def test_without_a_teacher_the_valid_part_of_an_answer_goes_on(
    client, dataset, native, monkeypatch
):
    from levi.agent.planning import approve
    from levi.agent.runtime import Workbench
    from levi.agent.schema import ModelOutput
    from levi.inference.provider import OllamaProvider

    def seg(content):
        return {
            "episode_index": 0,
            "kind": "segment",
            "content": content,
            "start": 0.0,
            "end": 0.1,
            "evidence_ids": ["x"],
            "outcome": "unknown",
        }

    def answer(self, *a, **k):
        output = ModelOutput(
            summary="s", proposals=[seg("good"), seg("bad"), seg("overlaps")]
        )
        return output, {"requests": 1, "tokens": 10}

    def check(context, proposals, evidence, summary):
        if any(p.content == "bad" for p in proposals):
            raise ValueError("bad proposal")
        if len(proposals) > 1:
            raise ValueError("Overlapping segments in an exclusive annotation layer")

    monkeypatch.setattr(OllamaProvider, "generate", answer)
    monkeypatch.setattr(Workbench, "validate_proposals", staticmethod(check))
    wb, context, _ = supervised_bench(client, dataset)
    context = context.model_copy(update={"supervision": "none", "teacher_grant": None})
    run = wb.plan(context)
    approve(wb, run["id"], 1, "human")
    done = execute_supervised(wb, run["id"])
    assert done["status"] == "waiting_for_review", done["reason"]
    shard = wb.store.get("shards", f"{run['id']}:0")
    assert [p["content"] for p in shard["output"]["proposals"]] == ["good"]
    events = [e for e in wb.store.events(run["id"]) if e["type"] == "answer_salvaged"]
    assert events and events[0]["dropped"] == 2


def _answers_by_episode(monkeypatch, bad_episodes):
    """A provider whose answer for ``bad_episodes`` fails validation outright."""
    from levi.agent.runtime import Workbench
    from levi.agent.schema import ModelOutput
    from levi.inference.provider import OllamaProvider

    def answer(self, config, goal, summary, evidence, artifacts, budget):
        episode = summary["episode_index"]
        content = "bad" if episode in bad_episodes else "good"
        proposal = {
            "episode_index": episode,
            "kind": "segment",
            "content": content,
            "start": 0.0,
            "end": 0.1,
            "evidence_ids": ["x"],
            "outcome": "unknown",
        }
        return ModelOutput(summary="s", proposals=[proposal]), {
            "requests": 1,
            "tokens": 10,
        }

    def check(context, proposals, evidence, summary):
        if any(p.content == "bad" for p in proposals):
            raise ValueError("bad proposal")

    monkeypatch.setattr(OllamaProvider, "generate", answer)
    monkeypatch.setattr(Workbench, "validate_proposals", staticmethod(check))


def _unsupervised_run(client, dataset):
    from levi.agent.planning import approve

    wb, context, _ = supervised_bench(client, dataset)
    context = context.model_copy(
        update={
            "supervision": "none",
            "teacher_grant": None,
            "episodes": [0, 1],
            "budget": Budget(max_calls=10),
            "workflow": {**context.workflow, "require_human_pilot": False},
        }
    )
    run = wb.plan(context)
    approve(wb, run["id"], 1, "human")
    assert wb.store.claim(run["id"], "test-run")
    return wb, run["id"]


def test_an_episode_with_no_valid_answer_is_set_aside_and_the_run_goes_on(
    client, dataset, native, monkeypatch
):
    _answers_by_episode(monkeypatch, bad_episodes={0})
    wb, run_id = _unsupervised_run(client, dataset)
    wb.execute(run_id, "test-run", pilot=False)
    run = wb.store.get("runs", run_id)
    assert run["status"] == "waiting_for_review", run["reason"]
    assert run["completed"] == [1]
    assert [(f["episode"], f["phase"]) for f in run["failed"]] == [(0, "coarse")]
    assert "bad proposal" in run["failed"][0]["reason"]
    events = [e for e in wb.store.events(run_id) if e["type"] == "episode_set_aside"]
    assert [e["episode"] for e in events] == [0]
    # The rejected answer stays beside the run for the review.
    assert (wb.store.run_dir(run_id) / "episode_000000-coarse-rejected.json").exists()
    # A resumed run does not ask the model about it again.
    wb.execute(run_id, "test-run", pilot=False)
    assert wb.store.get("runs", run_id)["failed"] == run["failed"]


def test_a_pilot_with_no_valid_answer_still_blocks(
    client, dataset, native, monkeypatch
):
    _answers_by_episode(monkeypatch, bad_episodes={0})
    wb, run_id = _unsupervised_run(client, dataset)
    wb.execute(run_id, "test-run", pilot=True)
    run = wb.store.get("runs", run_id)
    assert run["status"] == "blocked"
    assert run["reason"] == "bad proposal"
    assert run["failed"] == []


def test_too_many_set_aside_episodes_block_the_run(
    client, dataset, native, monkeypatch
):
    from levi.agent import runtime

    monkeypatch.setattr(runtime, "SET_ASIDE_MIN", 0)
    _answers_by_episode(monkeypatch, bad_episodes={0, 1})
    wb, run_id = _unsupervised_run(client, dataset)
    wb.execute(run_id, "test-run", pilot=False)
    run = wb.store.get("runs", run_id)
    assert run["status"] == "blocked"
    assert run["reason"].startswith("1 episodes failed validation (more than 0)")
    assert run["completed"] == []
