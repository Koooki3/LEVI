"""Bounded native Ollama protocol client.

No automatic pull, capability probe, inference or process startup is performed.
The caller supplies an authorized transport and remains responsible for task
scope, destination policy, credentials and resource ownership.
"""

import os
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class Transport(Protocol):
    def request(self, method: str, path: str, payload: dict | None) -> dict: ...
    def stream(self, method: str, path: str, payload: dict) -> Iterable[dict]: ...


class ModelDetails(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    digest: str
    size: int = Field(ge=0)


class OllamaError(RuntimeError):
    pass


def keep_alive():
    """How long the model stays in GPU memory after LEVI's last request.

    Ollama's own default is five minutes. LEVI's requests come back to back
    while a run works, so a shorter window frees the GPU soon after a run
    finishes, fails or waits for a person (``LEVI_OLLAMA_KEEP_ALIVE``: an
    Ollama duration such as ``2m``, or seconds; ``0`` unloads after every
    request)."""
    value = os.getenv("LEVI_OLLAMA_KEEP_ALIVE", "2m").strip()
    return int(value) if value.lstrip("-").isdigit() else value


class OllamaClient:
    def __init__(self, transport: Transport):
        self.transport = transport

    def _request(self, method, path, payload=None):
        response = self.transport.request(method, path, payload)
        if not isinstance(response, dict):
            raise OllamaError("Invalid response envelope")
        if response.get("error"):
            # Do not echo remote error bodies which may contain prompts/secrets.
            raise OllamaError("Ollama rejected the request")
        return response

    def version(self) -> str:
        version = self._request("GET", "/api/version").get("version")
        if not isinstance(version, str) or not version:
            raise OllamaError("Missing service version")
        return version

    def models(self) -> list[ModelDetails]:
        models = self._request("GET", "/api/tags").get("models")
        if not isinstance(models, list):
            raise OllamaError("Missing model inventory")
        return [ModelDetails.model_validate(item) for item in models]

    def show(self, model: str) -> dict:
        return self._request("POST", "/api/show", {"model": model})

    def require_model(self, model: str, digest: str) -> ModelDetails:
        for item in self.models():
            if item.name == model:
                if item.digest != digest:
                    raise OllamaError(
                        "Model digest changed; reauthorize the model profile"
                    )
                return item
        raise OllamaError(
            "Model is not installed; explicit download or import is required"
        )

    def pull(
        self,
        model: str,
        *,
        authorize: Callable[[], None],
        cancelled: Callable[[], bool],
    ) -> Iterable[dict]:
        """Stream real progress after authorization, then verify inventory.

        Cancellation stops consuming the stream; it does not claim to terminate
        a download shared with other Ollama clients or delete cached blobs.
        """
        authorize()
        if cancelled():
            raise OllamaError("Download cancelled before dispatch")
        events = self.transport.stream(
            "POST", "/api/pull", {"model": model, "stream": True}
        )
        complete = False
        try:
            for event in events:
                if cancelled():
                    raise OllamaError("Download cancellation requested")
                if not isinstance(event, dict) or event.get("error"):
                    raise OllamaError("Model download failed")
                status = event.get("status")
                if not isinstance(status, str):
                    raise OllamaError("Invalid download progress")
                progress: dict[str, Any] = {"status": status}
                for key in ("total", "completed"):
                    if key in event:
                        value = event[key]
                        if type(value) is not int or value < 0:
                            raise OllamaError("Invalid download byte count")
                        progress[key] = value
                if progress.get("completed", 0) > progress.get("total", float("inf")):
                    raise OllamaError("Download progress exceeds total")
                if "digest" in event:
                    progress["digest"] = event["digest"]
                if status == "success":
                    complete = True
                    break
                yield progress
        finally:
            close = getattr(events, "close", None)
            if close:
                close()
        if not complete:
            raise OllamaError("Download stream ended before completion")
        inventory = self.models()
        installed = next((item for item in inventory if item.name == model), None)
        if installed is None:
            raise OllamaError("Download completed but model is absent from inventory")
        yield {"status": "success", "model": installed.model_dump()}

    def chat(
        self,
        model: str,
        digest: str,
        messages: list[dict],
        *,
        output_schema: dict,
        max_output_tokens: int,
        context_tokens: int = 8192,
        think: bool = False,
    ) -> dict:
        if (
            max_output_tokens <= 0
            or context_tokens <= 0
            or max_output_tokens > context_tokens
        ):
            raise ValueError("Invalid token limits")
        self.require_model(model, digest)
        response = self._request(
            "POST",
            "/api/chat",
            {
                "model": model,
                "messages": messages,
                "format": output_schema,
                "stream": False,
                # Reasoning tokens before a schema-bound answer cost time and
                # tokens the answer rarely needs; opt in per call.
                "think": think,
                "keep_alive": keep_alive(),
                "options": {
                    "num_predict": max_output_tokens,
                    "num_ctx": context_tokens,
                },
            },
        )
        if response.get("done") is not True:
            raise OllamaError("Incomplete model response")
        message = response.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise OllamaError("Missing model response content")
        counts = [response.get("prompt_eval_count"), response.get("eval_count")]
        reported = all(type(value) is int and value >= 0 for value in counts)
        return {
            "content": message["content"],
            "tool_calls": message.get("tool_calls", []),
            "usage": {
                "tokens": sum(counts) if reported else None,
                "prompt_tokens": counts[0] if reported else None,
                "source": "reported" if reported else "unknown",
            },
        }
