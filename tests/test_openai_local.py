"""A local OpenAI-compatible server (vLLM) on LEVI's hardened local-model path,
driven by protocol fakes -- never a real server, model or GPU."""

import base64
import builtins
import json

import cv2
import httpx
import numpy as np
import pytest

from levi.agent.schema import Budget, ProviderConfig, TaskContext
from levi.agent.store import Store, file_hash
from levi.inference import models, provider
from levi.inference.ollama import OllamaError
from levi.inference.openai_local import OpenAILocalClient, served_digest, to_openai
from levi.inference.transport import OllamaTransport, OpenAILocalTransport

SERVED = {
    "id": "qwen3.8-27b",
    "object": "model",
    "owned_by": "vllm",
    "root": "/models/Qwen3.8-27B-INT4/snapshots/0123abcd",
    "max_model_len": 32768,
}
DIGEST = served_digest(SERVED)
DEFINITION = {
    "id": "move",
    "label": "Move",
    "definition": "Move gripper",
    "starts_when": "Motion starts",
    "ends_when": "Motion ends",
    "success_when": "Target visibly reached",
}
WORKFLOW = {"kind": "temporal", "definitions": [DEFINITION]}


def completion(content, usage=True, finish="stop", **message):
    return {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, **message},
                "finish_reason": finish,
            }
        ],
        **({"usage": {"prompt_tokens": 100, "completion_tokens": 20}} if usage else {}),
    }


def answer(payload):
    """What a well-behaved server returns: one interval over the summary's
    span, or a one-token calibration reply."""
    user = payload["messages"][-1]["content"]
    parts = user if isinstance(user, list) else [{"type": "text", "text": user}]
    text = next(p["text"] for p in parts if p["type"] == "text")
    images = sum(1 for p in parts if p["type"] == "image_url")
    usage = {"prompt_tokens": len(text) // 4 + 300 * images, "completion_tokens": 1}
    if payload["max_completion_tokens"] == 1:
        return {**completion("{"), "usage": usage}
    view = json.loads(text[text.index('{"goal"') :])
    summary, evidence = view["summary"], view["evidence"]
    content = {
        "proposals": [
            {
                "episode_index": summary["episode_index"],
                "kind": "segment",
                "subtask_id": "move",
                "start": summary["start"],
                "end": summary["end"],
                "outcome": "unknown",
                "evidence_note": "The gripper moves across the frames.",
                "evidence_ids": [evidence[0]["id"]],
                "content": "Move",
            }
        ],
        "summary": "One motion",
        "warnings": [],
    }
    return {
        **completion(json.dumps(content)),
        "usage": {**usage, "completion_tokens": 40},
    }


class FakeServer:
    """What vLLM answers on /v1/models, /version and /v1/chat/completions."""

    def __init__(self):
        self.calls = []
        self.served = [dict(SERVED)]
        self.reply = answer

    def request(self, method, path, payload):
        self.calls.append((method, path, payload))
        if path == "/v1/models":
            return {"object": "list", "data": self.served}
        if path == "/version":
            return {"version": "0.30.0"}
        if path == "/v1/chat/completions":
            return self.reply(payload)
        raise AssertionError(path)

    def stream(self, method, path, payload):
        raise AssertionError("A local server is never streamed from")

    def chats(self):
        return [payload for _, path, payload in self.calls if "chat" in path]


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
def server(monkeypatch):
    fake = FakeServer()
    factory = lambda config, **kwargs: OpenAILocalClient(fake, config)
    monkeypatch.setattr(models, "client_for", factory)
    monkeypatch.setattr(provider, "client_for", factory)
    return fake


def config(**kwargs):
    return ProviderConfig(
        **(
            {
                "name": "vllm",
                "kind": "openai-local",
                "base_url": "http://127.0.0.1:8000",
                "model": "qwen3.8-27b",
                "model_digest": DIGEST,
                "context_tokens": 32768,
                "vision": True,
                "structured_output": True,
                "allow_localhost": True,
            }
            | kwargs
        )
    )


def png(path, width, height, value=0):
    image = np.full((height, width, 3), value, dtype=np.uint8)
    image[0, 0] = (value + 1) % 256
    assert cv2.imwrite(str(path), image)
    return path


def evidence_dir(tmp_path, sizes=((10, 8), (12, 8), (14, 8))):
    folder = tmp_path / "evidence"
    folder.mkdir(exist_ok=True)
    rows = []
    for number, (width, height) in enumerate(sizes):
        png(folder / f"f{number}.png", width, height, value=40 * number)
        rows.append(
            {
                "id": f"front-{number}",
                "artifact": f"f{number}.png",
                "timestamp": number * 0.5,
                "sha256": file_hash(folder / f"f{number}.png"),
            }
        )
    # A text row between the images does not take an image number.
    rows.insert(1, {"id": "note", "artifact": None, "text": "state"})
    return folder, rows


def summary():
    return {"episode_index": 0, "start": 0.0, "end": 1.0, "workflow": WORKFLOW}


def sent_images(payload):
    parts = payload["messages"][-1]["content"]
    return [
        base64.b64decode(p["image_url"]["url"].split(",", 1)[1])
        for p in parts
        if p["type"] == "image_url"
    ]


def test_the_request_carries_the_narrowed_schema_thinking_off_and_images_in_order(
    tmp_path, server
):
    from levi.inference.provider import citable, learner_schema, output_allowance

    folder, rows = evidence_dir(tmp_path)
    output, spent = provider.LocalProvider().generate(
        config(), "Annotate", summary(), rows, folder, Budget()
    )
    [payload] = server.chats()
    schema = payload["response_format"]["json_schema"]
    assert schema["schema"] == learner_schema(WORKFLOW, None, citable(summary(), rows))
    assert schema["strict"] is True
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert "tools" not in payload and payload["stream"] is False
    assert payload["max_completion_tokens"] == min(
        output_allowance(config()), Budget().max_tokens
    )
    system, user = payload["messages"]
    assert system["role"] == "system" and user["role"] == "user"
    # Images first, in evidence order: image n is the row numbered n.
    assert sent_images(payload) == [
        (folder / f"f{n}.png").read_bytes() for n in range(3)
    ]
    assert user["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    view = json.loads(user["content"][-1]["text"])["evidence"]
    assert [row.get("image") for row in view] == [1, None, 2, 3]
    assert output.proposals[0].subtask_id == "move"
    assert spent["usage_kind"] == "reported"
    assert spent["prompt_tokens"] + 40 == spent["tokens"] == spent["reported_tokens"]
    # A profile that lets the model reason says so on the request.
    provider.LocalProvider().generate(
        config(think=True), "Annotate", summary(), rows, folder, Budget()
    )
    assert server.chats()[-1]["chat_template_kwargs"] == {"enable_thinking": True}


def test_usage_is_reported_or_unknown_never_zero(server):
    client = OpenAILocalClient(server, config())
    message = [{"role": "user", "content": "hi"}]
    server.reply = lambda payload: completion("{}")
    result = client.chat(
        "qwen3.8-27b", DIGEST, message, output_schema={}, max_output_tokens=10
    )
    assert result["usage"] == {
        "tokens": 120,
        "prompt_tokens": 100,
        "source": "reported",
    }
    server.reply = lambda payload: completion("{}", usage=False)
    result = client.chat(
        "qwen3.8-27b", DIGEST, message, output_schema={}, max_output_tokens=10
    )
    assert result["usage"] == {
        "tokens": None,
        "prompt_tokens": None,
        "source": "unknown",
    }


def test_other_weights_or_a_missing_model_block_inference(server):
    client = OpenAILocalClient(server, config())
    message = [{"role": "user", "content": "hi"}]
    server.served[0]["root"] = "/models/Qwen3.8-27B-INT4/snapshots/fedcba98"
    with pytest.raises(OllamaError, match="digest changed"):
        client.chat(
            "qwen3.8-27b", DIGEST, message, output_schema={}, max_output_tokens=9
        )
    server.served = []
    with pytest.raises(OllamaError, match="not served"):
        client.chat(
            "qwen3.8-27b", DIGEST, message, output_schema={}, max_output_tokens=9
        )
    assert server.chats() == []


def test_reasoning_without_an_answer_is_named_not_echoed(server):
    client = OpenAILocalClient(server, config())
    message = [{"role": "user", "content": "hi"}]
    server.reply = lambda payload: completion(None, reasoning_content="secret plan")
    with pytest.raises(OllamaError, match="reasoning but no answer.*think") as caught:
        client.chat(
            "qwen3.8-27b", DIGEST, message, output_schema={}, max_output_tokens=9
        )
    assert "secret" not in str(caught.value)
    server.reply = lambda payload: completion(None, finish="length", reasoning="x")
    with pytest.raises(OllamaError, match="ran out while reasoning"):
        client.chat(
            "qwen3.8-27b", DIGEST, message, output_schema={}, max_output_tokens=9
        )


def test_a_cut_off_answer_is_kept_its_tokens_settled_and_its_valid_part_salvaged(
    tmp_path, server
):
    from levi.inference.provider import InvalidAnswer

    folder, rows = evidence_dir(tmp_path)
    server.reply = lambda payload: completion(
        '{"proposals": [{"episode_index": 0, "kind": "segm', finish="length"
    )
    with pytest.raises(InvalidAnswer) as caught:
        provider.LocalProvider().generate(
            config(), "Annotate", summary(), rows, folder, Budget()
        )
    assert caught.value.usage["tokens"] == 120 and caught.value.salvaged is None
    assert '"segm' in (tmp_path / "episode_000000-coarse-rejected.json").read_text()

    def half_right(payload):
        good = json.loads(answer(payload)["choices"][0]["message"]["content"])
        good["proposals"].append({"kind": "subtask"})
        return completion(json.dumps(good), finish="length")

    server.reply = half_right
    with pytest.raises(InvalidAnswer) as caught:
        provider.LocalProvider().generate(
            config(), "Annotate", summary(), rows, folder, Budget()
        )
    assert [p.subtask_id for p in caught.value.salvaged.proposals] == ["move"]


@pytest.mark.parametrize(
    "body,expected",
    [
        (
            (
                "This model's maximum context length is 32768 tokens. However, you "
                "requested 40000 tokens (35000 in the messages, 5000 in the "
                "completion)."
            ),
            (
                "prompt (35000 tokens) and the answer allowance (5000) exceed "
                "the model context of 32768 tokens"
            ),
        ),
        (
            (
                "This model's maximum context length is 32768 tokens. However, "
                "you requested 2048 output tokens and your prompt contains 35000 "
                "input tokens, for a total of 37048 tokens."
            ),
            (
                "prompt (35000 tokens) and the answer allowance (2048) exceed "
                "the model context of 32768 tokens"
            ),
        ),
        # The prompt alone fits; with the answer allowance it does not.
        (
            (
                "This model's maximum context length is 32768 tokens. However, "
                "you requested 4096 output tokens and your prompt contains at "
                "least 30000 input tokens, for a total of at least 34096 tokens."
            ),
            (
                "prompt (30000 tokens) and the answer allowance (4096) exceed "
                "the model context of 32768 tokens"
            ),
        ),
        (
            (
                "This model's maximum context length is 32768 tokens. However, "
                "you requested 2048 output tokens and your prompt contains "
                "200000 characters (more than 122880 characters, which is the "
                "upper bound for 30720 input tokens)."
            ),
            "200000 characters, more than the model context of 32768 tokens",
        ),
        (
            (
                "The decoder prompt (length 41210) is longer than the maximum "
                "model length of 32768. Make sure that `max_model_len` is no "
                "smaller than the number of text tokens plus multimodal tokens."
            ),
            "needs 41210 tokens but the model context holds 32768",
        ),
        ("At most 4 image(s) may be provided in one prompt.", "max_images to 4"),
        (
            "Conversation roles must alternate user/assistant/user/assistant/...",
            "fold_system",
        ),
        ("xgrammar does not support minItems", "structured-output backend"),
        ("something else went wrong", None),
    ],
)
def test_server_errors_are_rephrased_without_echoing_the_body(body, expected):
    transport = OpenAILocalTransport(config())
    envelope = {"object": "error", "message": body + " secret prompt", "code": 400}
    transport._client = lambda: httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(400, json=envelope))
    )
    with pytest.raises(OllamaError) as caught:
        transport.request("POST", "/v1/chat/completions", {})
    message = str(caught.value)
    assert message.startswith("Local model server HTTP request failed (400)")
    assert "secret" not in message
    if expected:
        assert expected in message
    else:
        assert message == "Local model server HTTP request failed (400)"


def test_an_error_body_is_read_only_as_far_as_its_head():
    pulled = []

    def huge():
        for _ in range(4096):
            pulled.append(1)
            yield b"x" * 1024

    transport = OpenAILocalTransport(config())
    transport._client = lambda: httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(500, content=huge()))
    )
    with pytest.raises(OllamaError, match=r"failed \(500\)"):
        transport.request("POST", "/v1/chat/completions", {})
    assert len(pulled) <= 5


def test_only_the_server_api_is_reachable_and_only_on_loopback():
    from levi.inference.transport import validate_local_endpoint

    # Refused before any connection (the fixture turns one into an error).
    with pytest.raises(OllamaError):
        OpenAILocalTransport(config()).request("POST", "/api/pull", {})
    for url, allowed in (
        ("http://127.0.0.1:8000/v1", True),
        ("http://127.0.0.1:8000", False),
        ("http://user:pass@127.0.0.1:8000", True),
    ):
        with pytest.raises(ValueError):
            validate_local_endpoint(url, allowed, "Local model server")


def test_the_key_is_optional_its_own_and_sent_only_when_set(monkeypatch):
    from levi.agent import credentials

    # A cloud key set for an online profile is never sent to a local port.
    monkeypatch.setenv("LEVI_MODEL_API_KEY", "cloud-key")
    monkeypatch.delenv("LEVI_LOCAL_MODEL_KEY", raising=False)
    profile = ProviderConfig.model_validate(
        {
            "name": "vllm",
            "kind": "openai-local",
            "base_url": "http://127.0.0.1:8000",
            "model": "qwen3.8-27b",
            "allow_localhost": True,
        }
    )
    assert profile.key_env == "LEVI_LOCAL_MODEL_KEY"
    assert credentials.status(profile) == "not_required"
    seen = []

    def wire(client):
        client.transport._client = lambda: httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (
                    seen.append(request.headers.get("authorization"))
                    or httpx.Response(200, json={"object": "list", "data": [SERVED]})
                )
            )
        )
        return client

    wire(provider.client_for(profile)).models()
    monkeypatch.setenv("LEVI_LOCAL_MODEL_KEY", "local-key")
    assert credentials.status(profile) == "environment"
    client = wire(provider.client_for(profile))
    assert isinstance(client, OpenAILocalClient)
    client.models()
    assert seen == [None, "Bearer local-key"]


def test_max_images_is_refused_before_the_gpu_or_the_server(
    tmp_path, server, monkeypatch
):
    from levi.agent.observations import ContextOverflow, image_limit
    from levi.inference import gpu

    folder, rows = evidence_dir(tmp_path)

    def untouched(config=None):
        raise AssertionError("the GPU guard is not consulted for a refused request")

    monkeypatch.setattr(gpu, "require_free", untouched)
    with pytest.raises(ContextOverflow, match="max_images"):
        provider.LocalProvider().generate(
            config(max_images=2), "Annotate", summary(), rows, folder, Budget()
        )
    assert server.calls == []
    # Refinement batches are sized to the server's limit too.
    usage = {"reported_tokens": None}
    assert image_limit(config(max_images=5), usage, rows, {}) == 5
    assert image_limit(config(max_images=5), usage, rows, {}, costs=(0.25, 300)) == 5
    assert image_limit(config(), usage, rows, {}, costs=(0.25, 300)) > 5


def test_image_max_side_scales_only_the_copy_sent(tmp_path, server, monkeypatch):
    from levi.inference import request_cost

    folder, rows = evidence_dir(tmp_path, sizes=((640, 480), (320, 240)))
    before = [file_hash(folder / row["artifact"]) for row in rows if row["artifact"]]
    provider.LocalProvider().generate(
        config(image_max_side=160), "Annotate", summary(), rows, folder, Budget()
    )
    shapes = [
        cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR).shape[:2]
        for data in sent_images(server.chats()[0])
    ]
    assert shapes == [(120, 160), (120, 160)]
    assert before == [
        file_hash(folder / row["artifact"]) for row in rows if row["artifact"]
    ]
    # Calibration prices the images as they are sent, under its own key.
    priced = []

    class Client:
        def chat(self, model, digest, messages, **options):
            priced.extend(messages[0].get("images", []))
            return {"usage": {"tokens": 10, "prompt_tokens": 9}}

    monkeypatch.setattr(provider, "client_for", lambda *a, **k: Client())
    frames = [folder / row["artifact"] for row in rows if row["artifact"]]
    request_cost.calibrate(config(image_max_side=160), "x" * 100, frames)
    assert [
        cv2.imdecode(
            np.frombuffer(base64.b64decode(data), np.uint8), cv2.IMREAD_COLOR
        ).shape[:2]
        for data in priced
    ] == [(120, 160), (120, 160)]
    assert request_cost._key(config(image_max_side=160)).endswith("@160px")
    assert request_cost._key(config()) == f"qwen3.8-27b@{DIGEST}"


def test_fold_system_sends_one_user_turn(server):
    image = base64.b64encode(b"\xff\xd8\xff fixture").decode()
    messages = [
        {"role": "system", "content": "Rules"},
        {"role": "user", "content": "Task", "images": [image]},
    ]
    assert to_openai(messages, fold_system=True) == [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image}"},
                },
                {"type": "text", "text": "Rules\n\nTask"},
            ],
        }
    ]
    assert [m["role"] for m in to_openai(messages)] == ["system", "user"]
    server.reply = lambda payload: completion("{}")
    OpenAILocalClient(server, config(fold_system=True)).chat(
        "qwen3.8-27b",
        DIGEST,
        messages[:1] + [{"role": "user", "content": "Task"}],
        output_schema={},
        max_output_tokens=9,
    )
    assert server.chats()[0]["messages"] == [
        {"role": "user", "content": "Rules\n\nTask"}
    ]


def test_the_router_sends_openai_local_to_the_local_provider(monkeypatch):
    from levi.agent.providers import RoutedProvider
    from levi.inference.provider import LocalProvider

    called = []
    monkeypatch.setattr(
        LocalProvider, "generate", lambda self, config, *a: called.append(config.kind)
    )
    RoutedProvider().generate(config(), "x", {}, [], None, Budget())
    assert called == ["openai-local"]


def test_inspect_bind_and_the_operations_a_server_owns(client, server, monkeypatch):
    from levi.agent import credentials

    monkeypatch.delenv("LEVI_LOCAL_MODEL_KEY", raising=False)
    monkeypatch.setattr(credentials, "_KEYS", {})
    profile = config(model_digest=None).model_dump()
    assert client.post("/api/levi/agent/v1/providers", json=profile).status_code == 200
    listed = client.get("/api/levi/agent/v1/providers").json()[0]
    assert listed["credential_ready"] and listed["credential_source"] == "not_required"
    # A server started with --api-key: the key given for this session.
    keyed = client.post(
        "/api/levi/agent/v1/providers/vllm/session", json={"key": "sk-local"}
    )
    assert keyed.status_code == 200
    status = client.get("/api/levi/agent/v1/providers/vllm/ollama").json()
    assert status["model"]["digest"] == DIGEST
    assert status["model"]["max_model_len"] == 32768
    assert status["capability_evidence"] == "user_confirmed"
    assert status["version"] == "0.30.0"
    bound = client.post(
        "/api/levi/agent/v1/providers/vllm/ollama/bind",
        json={"digest": DIGEST, "vision": True, "structured_output": True},
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["model_digest"] == DIGEST and bound.json()["vision"]
    assert server.chats() == []
    # Binding records the digest in the profile; the key still goes with it.
    listed = client.get("/api/levi/agent/v1/providers").json()[0]
    assert listed["credential_source"] == "session"
    assert credentials.get(ProviderConfig.model_validate(bound.json())) == "sk-local"
    for path, body in (
        ("memory", {"operation": "load", "approve_hardware_use": True}),
        ("download", {"request_id": "x", "approve_download": True}),
    ):
        response = client.post(
            f"/api/levi/agent/v1/providers/vllm/ollama/{path}", json=body
        )
        assert response.status_code == 409, response.text
    # The profile's context is the server's, whatever it said: no request
    # can narrow it, so it is what one call may spend and reserves.
    for asked in (16384, 65536):
        profile = config(model_digest=None, context_tokens=asked).model_dump()
        client.post("/api/levi/agent/v1/providers", json=profile)
        bound = client.post(
            "/api/levi/agent/v1/providers/vllm/ollama/bind",
            json={"digest": DIGEST, "vision": True, "structured_output": True},
        )
        assert bound.status_code == 200 and bound.json()["context_tokens"] == 32768
    # ...and one longer than LEVI reserves per call is refused.
    server.served[0]["max_model_len"] = 262144
    refused = client.post(
        "/api/levi/agent/v1/providers/vllm/ollama/bind",
        json={
            "digest": served_digest(server.served[0]),
            "vision": True,
            "structured_output": True,
        },
    )
    assert refused.status_code == 400 and "max-model-len" in refused.json()["detail"]
    # A base URL with /v1 is ambiguous: the server root is the profile's.
    bad = config(base_url="http://127.0.0.1:8000/v1").model_dump()
    assert client.post("/api/levi/agent/v1/providers", json=bad).status_code == 400


def camera_dataset(dataset):
    camera = "observation.images.front"
    info_path = dataset / "meta/info.json"
    info = json.loads(info_path.read_text())
    info["features"][camera] = {"dtype": "video", "shape": [48, 64, 3]}
    info_path.write_text(json.dumps(info))
    directory = dataset / f"videos/chunk-000/{camera}"
    directory.mkdir(parents=True)
    for ep in range(2):
        writer = cv2.VideoWriter(
            str(directory / f"episode_{ep:06d}.mp4"),
            cv2.VideoWriter_fourcc(*"mp4v"),
            10,
            (64, 48),
        )
        for frame in range(20):
            writer.write(np.full((48, 64, 3), frame * 10, dtype=np.uint8))
        writer.release()
    return camera


def test_an_unbound_server_cannot_plan_and_a_bound_one_runs_coarse_calibrate_refine(
    client, dataset, server
):
    from levi import catalog, service
    from levi.agent.planning import approve
    from levi.agent.runtime import Workbench

    camera = camera_dataset(dataset)
    entry = catalog.register(str(dataset))
    wb = Workbench(service.STATE)
    context = TaskContext(
        repo_id=entry["id"],
        episodes=[0],
        instruction="Mark the motion",
        provider="vllm",
        cameras=[camera],
        allow_media_egress=True,
        workflow={**WORKFLOW, "coarse_step_seconds": 0.5},
        budget=Budget(max_calls=12, max_tokens=None, max_seconds=600),
    )
    wb.store.put("providers", "vllm", config(model_digest=None).model_dump())
    with pytest.raises(ValueError, match="digest"):
        wb.plan(context)
    assert server.calls == []
    wb.store.put("providers", "vllm", config(fold_system=True).model_dump())
    run = wb.plan(context)
    approve(wb, run["id"], 1, "human")
    assert wb.store.claim(run["id"], "test-owner")
    wb.execute(run["id"], "test-owner", pilot=True)
    result = wb.store.get("runs", run["id"])
    assert result["status"] == "waiting_for_review", result
    events = wb.store.events(run["id"])
    assert any(e["type"] == "request_cost_calibrated" for e in events)
    steps = [e for e in events if e["type"] == "model_step"]
    assert [e["phase"] for e in steps] == ["coarse", "refine"]
    assert all(e["usage"]["usage_kind"] == "reported" for e in steps)
    chats = server.chats()
    assert [c["max_completion_tokens"] for c in chats].count(1) == 2
    # Molmo-style profile: every request is one user turn, no system role.
    assert all(len(c["messages"]) == 1 for c in chats)
    assert result["tokens"] > 0 and result["requests"] == 4


def test_a_sentence_becomes_a_task_on_a_local_server(tmp_path, server, monkeypatch):
    from levi.harness import tasking

    monkeypatch.setattr(
        tasking,
        "catalog_digest",
        lambda: [
            {
                "id": "local/plates",
                "episodes": 12,
                "fps": 10,
                "cameras": ["view1"],
                "kind": "raw",
            }
        ],
    )
    spec = {
        "dataset": "local/plates",
        "steps": [{"kind": "quality"}],
        "report": ["tokens"],
    }
    server.reply = lambda payload: completion(json.dumps(spec))
    store = Store(tmp_path)
    store.put("providers", "vllm", config(model_digest=None).model_dump())
    with pytest.raises(ValueError, match="bound local model"):
        tasking.interpret(store, "check the plates", "vllm")
    store.put("providers", "vllm", config().model_dump())
    task = tasking.interpret(store, "check the plates", "vllm")
    assert task["spec"]["dataset"] == "local/plates", task
    assert task["interpretation"]["tokens"] == 120
    [payload] = server.chats()
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["response_format"]["json_schema"]["schema"] == (
        tasking.TaskSpec.model_json_schema()
    )
    store.put("providers", "vllm", config(think=True).model_dump())
    tasking.interpret(store, "check the plates", "vllm")
    assert server.chats()[-1]["chat_template_kwargs"] == {"enable_thinking": True}


def test_cost_and_evaluation_count_a_local_server_as_local():
    from levi.eval import record
    from levi.harness import cost

    run = {
        "context": {"supervision": "none"},
        "provider_config": {"kind": "openai-local"},
    }
    assert cost.provider_kind(run) == "openai-local"
    assert record.driver(run) == "local-vlm"
    run["context"]["supervision"] = "supervised"
    assert record.driver(run) == "local-vlm-teacher"
