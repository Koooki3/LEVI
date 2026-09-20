"""Optional SDK contract tests; transport responses are fixtures, never models."""

import builtins
import importlib
import inspect
import json
import socket

import pytest


@pytest.fixture(autouse=True)
def offline_no_accelerators(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"torch", "sam3", "jax", "tensorflow"}:
            raise AssertionError("Accelerator/model import forbidden")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)

    def no_socket(*args, **kwargs):
        raise AssertionError("External network forbidden in SDK tests")

    monkeypatch.setattr(socket.socket, "connect", no_socket)


def test_compatible_provider_uses_pinned_transport_and_typed_output(
    monkeypatch, tmp_path
):
    pytest.importorskip("pydantic_ai")
    from openai import AsyncOpenAI

    from levi.agent import providers
    from levi.agent.schema import Budget, ProviderConfig

    module = (
        "httpx2"
        if "httpx2"
        in str(inspect.signature(AsyncOpenAI).parameters["http_client"].annotation)
        else "httpx"
    )
    http = importlib.import_module(module)
    observed = []

    async def respond(self, request):
        payload = json.loads(request.content)
        observed.append((request, payload))
        name = next(
            t["function"]["name"]
            for t in payload["tools"]
            if "proposals" in t["function"]["parameters"].get("properties", {})
        )
        return http.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "result",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(
                                            {
                                                "summary": "Offline fixture",
                                                "proposals": [],
                                                "warnings": [],
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            },
            request=request,
        )

    monkeypatch.setattr(http.AsyncHTTPTransport, "handle_async_request", respond)
    monkeypatch.setattr(providers, "endpoint_addresses", lambda *a: {"93.184.216.34"})
    monkeypatch.setenv("LEVI_TEST_KEY", "fixture-not-a-real-key")
    output, usage = providers.CompatibleProvider().generate(
        ProviderConfig(
            name="fixture",
            base_url="https://example.invalid/v1",
            model="fixture",
            key_env="LEVI_TEST_KEY",
            tools=True,
        ),
        "Inspect",
        {"episode_index": 0},
        [],
        tmp_path,
        Budget(),
    )
    assert output.summary == "Offline fixture"
    assert usage["requests"] == 1
    assert observed[0][0].url.host == "93.184.216.34"
    assert observed[0][0].headers["host"] == "example.invalid"
    assert "fixture-not-a-real-key" not in json.dumps(observed[0][1])


def test_mcp_server_builds_offline():
    pytest.importorskip("mcp")
    from mcp import types

    from levi.agent.mcp import build_server

    server = build_server()
    for request in (
        types.ListToolsRequest,
        types.CallToolRequest,
        types.ListResourcesRequest,
        types.ReadResourceRequest,
        types.ListPromptsRequest,
        types.GetPromptRequest,
    ):
        assert request in server.request_handlers


def test_mcp_roundtrip_preserves_scope_and_exposes_skills(monkeypatch, tmp_path):
    import asyncio

    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session

    from levi.agent import mcp as bridge
    from levi.agent.capabilities import invoke
    from levi.agent.runtime import Workbench
    from levi.agent.security import Principal

    wb = Workbench(tmp_path)
    who = Principal("fixture-client", datasets=("local/fixture",))

    def request(path, payload=None):
        if path == "capabilities":
            return invoke(wb, who, "capabilities.list", {})
        return invoke(wb, who, payload["name"], payload["arguments"])

    monkeypatch.setattr(bridge, "request", request)

    async def check():
        async with create_connected_server_and_client_session(
            bridge.build_server()
        ) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools.tools}
            assert "runs__plan" in names and "media__sample" in names
            assert "changes__approve" not in names and "changes__commit" not in names
            result = await client.call_tool("capabilities__list", {})
            assert not result.isError
            denied = await client.call_tool("objects.run", {"job_id": "does-not-exist"})
            assert denied.isError
            resources = await client.list_resources()
            assert len(resources.resources) == 6
            content = await client.read_resource(resources.resources[0].uri)
            assert content.contents
            prompts = await client.list_prompts()
            assert prompts.prompts[0].name == "review-dataset"
            assert (await client.get_prompt("review-dataset")).messages

    asyncio.run(check())
