"""Native Ollama protocol tests use in-memory transports exclusively."""

import pytest

from levi.inference.ollama import OllamaClient, OllamaError


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.inventory = [
            {"name": "qwen3.5:4b", "digest": "fixture-digest", "size": 10}
        ]
        self.events = [
            {"status": "pulling", "completed": 2, "total": 10},
            {"status": "success"},
        ]
        self.response = {"done": True, "message": {"content": "{}"}}
        self.closed = False

    def request(self, method, path, payload):
        self.calls.append((method, path, payload))
        if path == "/api/tags":
            return {"models": self.inventory}
        if path == "/api/version":
            return {"version": "fixture"}
        return self.response

    def stream(self, method, path, payload):
        self.calls.append((method, path, payload))
        try:
            yield from self.events
        finally:
            self.closed = True


def test_missing_model_does_not_pull_or_infer():
    transport = FakeTransport()
    transport.inventory = []
    client = OllamaClient(transport)
    with pytest.raises(OllamaError, match="not installed"):
        client.chat(
            "qwen3.5:4b", "fixture-digest", [], output_schema={}, max_output_tokens=100
        )
    assert [row[1] for row in transport.calls] == ["/api/tags"]


def test_model_update_blocks_inference():
    transport = FakeTransport()
    with pytest.raises(OllamaError, match="digest changed"):
        OllamaClient(transport).require_model("qwen3.5:4b", "older-digest")
    assert len(transport.calls) == 1


def test_pull_requires_authorization_and_validates_completion():
    transport = FakeTransport()
    client = OllamaClient(transport)

    def deny():
        raise PermissionError("No model installation grant")

    with pytest.raises(PermissionError):
        list(client.pull("qwen3.5:4b", authorize=deny, cancelled=lambda: False))
    assert transport.calls == []
    events = list(
        client.pull("qwen3.5:4b", authorize=lambda: None, cancelled=lambda: False)
    )
    assert events[0]["completed"] == 2
    assert events[-1]["model"]["digest"] == "fixture-digest"
    assert transport.closed


@pytest.mark.parametrize(
    "events",
    [
        [],
        [{"status": "pulling"}],
        [{"status": "pulling", "completed": 11, "total": 10}],
        [{"error": "secret"}],
    ],
)
def test_bad_or_interrupted_pull_is_not_success(events):
    transport = FakeTransport()
    transport.events = events
    with pytest.raises(OllamaError) as error:
        list(
            OllamaClient(transport).pull(
                "qwen3.5:4b", authorize=lambda: None, cancelled=lambda: False
            )
        )
    assert "secret" not in str(error.value)
    assert transport.closed


def test_cancel_does_not_start_download():
    transport = FakeTransport()
    with pytest.raises(OllamaError, match="cancelled"):
        list(
            OllamaClient(transport).pull(
                "qwen3.5:4b", authorize=lambda: None, cancelled=lambda: True
            )
        )
    assert transport.calls == []


def test_usage_missing_is_unknown_not_zero():
    transport = FakeTransport()
    client = OllamaClient(transport)
    result = client.chat(
        "qwen3.5:4b", "fixture-digest", [], output_schema={}, max_output_tokens=100
    )
    assert result["usage"] == {
        "tokens": None,
        "prompt_tokens": None,
        "source": "unknown",
    }
    transport.response.update(prompt_eval_count=100, eval_count=20)
    result = client.chat(
        "qwen3.5:4b", "fixture-digest", [], output_schema={}, max_output_tokens=100
    )
    # The prompt's share is kept apart: it is what sizing the next prompt needs.
    assert result["usage"] == {
        "tokens": 120,
        "prompt_tokens": 100,
        "source": "reported",
    }
    assert transport.calls[-1][2]["options"]["num_predict"] == 100
