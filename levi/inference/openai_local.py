"""Bounded client for a local OpenAI-compatible model server (vLLM first).

The same interface as ``OllamaClient`` -- ``version``, ``models``, ``show``,
``require_model``, ``chat`` -- so the hardened local path (narrowed answer
schema, compact evidence, request-cost sizing, salvage, GPU guard) serves
both. Like the Ollama client it performs no download, capability probe,
inference or process start of its own, and never echoes a remote body.

Such a server declares no model digest, capabilities or template. A model's
identity is what the server says it serves -- its id, the weights it was
started from (``root``: serve a snapshot directory and the revision is in it)
and its context -- hashed into a digest that the profile binds, so a server
restarted with other weights blocks inference until a person binds again.
"""

import hashlib
import json

from levi.agent.schema import MAX_IMAGE_PIXELS, MIN_IMAGE_PIXELS

from .ollama import ModelDetails, OllamaError, Transport


def served_digest(item: dict) -> str:
    """The 64-hex identity of one ``/v1/models`` entry."""
    identity = {key: item.get(key) for key in ("id", "root", "max_model_len")}
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode("utf-8")
    ).hexdigest()


def mm_processor_kwargs(max_pixels) -> dict | None:
    """The request's ``mm_processor_kwargs`` for a profile's ``max_pixels``, or
    None when the profile sets no budget (the request then carries no such
    key). vLLM takes the field at the top of the chat-completions body, beside
    ``chat_template_kwargs``, and hands it to the model's processor; Qwen3-VL
    reads ``max_pixels`` as the ceiling on one image's pixels. A value outside
    the profile's range is refused here as well, for a config that skipped
    ``ProviderConfig``'s validation."""
    if max_pixels is None:
        return None
    if (
        type(max_pixels) is not int
        or not MIN_IMAGE_PIXELS <= max_pixels <= MAX_IMAGE_PIXELS
    ):
        raise ValueError(
            f"Invalid max_pixels {max_pixels!r}: use an integer from "
            f"{MIN_IMAGE_PIXELS} (the processor's pixel floor) to {MAX_IMAGE_PIXELS}"
        )
    return {"max_pixels": max_pixels}


def _mime(data: str) -> str:
    return "image/jpeg" if data.startswith("/9j/") else "image/png"


def to_openai(messages: list[dict], *, fold_system: bool = False) -> list[dict]:
    """LEVI's messages (``content`` text, ``images`` base64) as OpenAI chat
    messages. A message's images come first, in evidence order -- image *n*
    is the evidence row with ``"image": n`` -- then its text, the layout
    Ollama gives the same request. ``fold_system`` sends the system prompt as
    the start of the first user turn, for templates that allow no system
    role and require strict user/assistant alternation (Molmo2)."""
    out, pending = [], []
    for message in messages:
        role, text = message["role"], message.get("content") or ""
        if fold_system and role == "system":
            pending.append(text)
            continue
        if pending and role == "user":
            text = "\n\n".join([*pending, text])
            pending = []
        images = message.get("images") or []
        content = (
            [
                *(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{_mime(data)};base64,{data}"},
                    }
                    for data in images
                ),
                {"type": "text", "text": text},
            ]
            if images
            else text
        )
        out.append({"role": role, "content": content})
    if pending:
        out.insert(0, {"role": "user", "content": "\n\n".join(pending)})
    return out


class OpenAILocalClient:
    def __init__(self, transport: Transport, config=None):
        self.transport = transport
        self.fold_system = bool(getattr(config, "fold_system", False))
        self.max_pixels = getattr(config, "max_pixels", None)

    def _request(self, method, path, payload=None):
        response = self.transport.request(method, path, payload)
        if not isinstance(response, dict):
            raise OllamaError("Invalid response envelope")
        if response.get("error") or response.get("object") == "error":
            # Do not echo remote error bodies which may contain prompts/secrets.
            raise OllamaError("The local model server rejected the request")
        return response

    def version(self) -> str:
        try:
            version = self._request("GET", "/version").get("version")
        except OllamaError:
            # Not every OpenAI-compatible server has one; models() is what
            # tells whether it is reachable at all.
            return "unknown"
        return version if isinstance(version, str) and version else "unknown"

    def models(self) -> list[ModelDetails]:
        data = self._request("GET", "/v1/models").get("data")
        if not isinstance(data, list):
            raise OllamaError("Missing model inventory")
        rows = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise OllamaError("Invalid model inventory entry")
            length = item.get("max_model_len")
            rows.append(
                ModelDetails(
                    name=item["id"],
                    digest=served_digest(item),
                    size=0,
                    root=item.get("root")
                    if isinstance(item.get("root"), str)
                    else None,
                    max_model_len=length if type(length) is int else None,
                )
            )
        return rows

    def show(self, model: str) -> dict:
        """What the server says about ``model``: its weights and context. It
        declares no capabilities; vision and structured output are the
        person's confirmation at bind time."""
        item = next((m for m in self.models() if m.name == model), None)
        details = (
            {"root": item.root, "max_model_len": item.max_model_len} if item else {}
        )
        return {"capabilities": [], "details": details, "template": ""}

    def require_model(self, model: str, digest: str) -> ModelDetails:
        for item in self.models():
            if item.name == model:
                if item.digest != digest:
                    raise OllamaError(
                        "Model digest changed; reauthorize the model profile"
                    )
                return item
        raise OllamaError(
            "Model is not served; start the local server with this model first"
        )

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
        budget = mm_processor_kwargs(self.max_pixels)  # refused before any request
        self.require_model(model, digest)
        response = self._request(
            "POST",
            "/v1/chat/completions",
            {
                "model": model,
                "messages": to_openai(messages, fold_system=self.fold_system),
                # Guided decoding: the server enforces the narrowed schema
                # while the answer is generated, as Ollama's "format" does.
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "levi_answer",
                        "schema": output_schema,
                        "strict": True,
                    },
                },
                "max_completion_tokens": max_output_tokens,
                "stream": False,
                # Qwen3-family templates think unless told not to.
                "chat_template_kwargs": {"enable_thinking": bool(think)},
                # Only a profile that sets a budget sends the key at all.
                **({} if budget is None else {"mm_processor_kwargs": budget}),
            },
        )
        choices = response.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        if not isinstance(choice, dict):
            raise OllamaError("Incomplete model response")
        message = choice.get("message")
        message = message if isinstance(message, dict) else {}
        if not isinstance(message.get("content"), str):
            if message.get("reasoning_content") or message.get("reasoning"):
                raise OllamaError(
                    "The model returned reasoning but no answer; "
                    + (
                        "its output allowance ran out while reasoning"
                        if choice.get("finish_reason") == "length"
                        else "set think to false on this profile, or serve "
                        "without --reasoning-parser"
                    )
                )
            raise OllamaError("Missing model response content")
        # A cut-off answer ("length") is not an error here: it fails
        # validation downstream and its valid proposals are salvaged.
        usage = response.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        counts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
        reported = all(type(value) is int and value >= 0 for value in counts)
        return {
            "content": message["content"],
            "tool_calls": message.get("tool_calls") or [],
            "finish_reason": choice.get("finish_reason"),
            "usage": {
                "tokens": sum(counts) if reported else None,
                "prompt_tokens": counts[0] if reported else None,
                "source": "reported" if reported else "unknown",
            },
        }
