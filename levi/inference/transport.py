"""Pinned, bounded loopback transport for a user-authorized local model
service: Ollama, or an OpenAI-compatible server such as vLLM."""

import contextlib
import contextvars
import ipaddress
import json
import socket
import threading
import time
from urllib.parse import urlsplit

from levi.agent.security import endpoint_addresses

from .ollama import OllamaError

# Stopping a run must stop the model computing for it. A request blocks for
# as long as the model generates, and Ollama (like vLLM) stops generating when
# the client goes away, so a run's requests carry its id (``requests_of``) and ``abort``
# shuts their sockets down. A plain close would not wake the blocked read.
_OWNER = contextvars.ContextVar("levi_model_requests", default=None)
_OPEN: dict[str, list[tuple[socket.socket, dict]]] = {}
_STOPPED: set[str] = set()
_LOCK = threading.Lock()


@contextlib.contextmanager
def requests_of(owner: str):
    """Model requests made inside belong to ``owner`` (a run id)."""
    with _LOCK:
        _STOPPED.discard(owner)
    token = _OWNER.set(owner)
    try:
        yield
    finally:
        _OWNER.reset(token)
        with _LOCK:
            _STOPPED.discard(owner)


def abort(owner: str) -> int:
    """Cut ``owner``'s model requests, including one that connects next;
    returns how many were in flight."""
    with _LOCK:
        _STOPPED.add(owner)
        open_ = list(_OPEN.get(owner, ()))
    for sock, state in open_:
        state["aborted"] = True
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
    return len(open_)


def _register(owner, state):
    """An httpcore trace hook that records the request's socket."""

    def trace(event, info):
        if event != "connection.connect_tcp.complete" or owner is None:
            return
        stream = info.get("return_value")
        sock = stream.get_extra_info("socket") if stream is not None else None
        if sock is None:
            return
        with _LOCK:
            _OPEN.setdefault(owner, []).append((sock, state))
            stopped = owner in _STOPPED
        if stopped:
            state["aborted"] = True
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    return trace


def _unregister(owner, state):
    with _LOCK:
        rows = [row for row in _OPEN.get(owner, ()) if row[1] is not state]
        if rows:
            _OPEN[owner] = rows
        else:
            _OPEN.pop(owner, None)


def validate_local_endpoint(
    url: str, allow_localhost: bool, label: str = "Ollama"
) -> list[str]:
    parsed = urlsplit(url)
    if not allow_localhost or parsed.path not in {"", "/"}:
        raise ValueError(
            f"{label} requires an explicitly allowed loopback service root "
            "(scheme, host and port only, no /v1)"
        )
    addresses = endpoint_addresses(url, allow_localhost=True)
    if not all(ipaddress.ip_address(address).is_loopback for address in addresses):
        raise ValueError(f"{label} endpoints must be loopback services")
    return sorted(addresses)


validate_ollama_endpoint = validate_local_endpoint


class OllamaTransport:
    """No redirects, environment proxies, automatic retries or implicit pulls.

    The client is created per request, closes on cancellation, pins DNS, and
    limits response bytes as well as total duration. Download bodies consist of
    progress only; model blobs stay inside the Ollama service's model directory.
    """

    label = "Ollama"
    paths = frozenset(
        {
            "/api/version",
            "/api/tags",
            "/api/show",
            "/api/pull",
            "/api/chat",
            "/api/ps",
            "/api/generate",
        }
    )

    def __init__(self, config, *, timeout=120, max_response_bytes=16 * 1024 * 1024):
        self.url = config.base_url.rstrip("/")
        self.allow_localhost = config.allow_localhost
        self.timeout = max(1, min(timeout, 3600))
        self.limit = max_response_bytes
        self.headers = {}

    def _client(self):
        import httpx

        addresses = validate_local_endpoint(self.url, self.allow_localhost, self.label)
        label = self.label
        parsed = urlsplit(self.url)
        address = addresses[0]

        class Pinned(httpx.HTTPTransport):
            def handle_request(self, request):
                if request.url.host != parsed.hostname:
                    raise ValueError(f"Unapproved {label} destination")
                request.headers["Host"] = parsed.netloc
                request.extensions["sni_hostname"] = parsed.hostname.encode()
                request.url = request.url.copy_with(host=address)
                return super().handle_request(request)

        return httpx.Client(
            transport=Pinned(retries=0),
            trust_env=False,
            follow_redirects=False,
            # Connect fast; the read may legitimately take as long as the call
            # (a first request loads the model, a vision answer takes seconds).
            timeout=httpx.Timeout(self.timeout, connect=min(30, self.timeout)),
        )

    def _chunks(self, method, path, payload):
        from httpx import HTTPError

        if path not in self.paths or method not in {"GET", "POST"}:
            raise ValueError(f"Unsupported {self.label} operation")
        started = time.monotonic()
        total = 0
        owner = _OWNER.get()
        state = {"aborted": False}
        try:
            with (
                self._client() as client,
                client.stream(
                    method,
                    self.url + path,
                    json=payload,
                    headers=self.headers,
                    extensions={"trace": _register(owner, state)},
                ) as response,
            ):
                if response.status_code != 200:
                    raise OllamaError(
                        f"{self.label} HTTP request failed ({response.status_code})"
                        + _known_cause(response)
                    )
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self.limit or time.monotonic() - started > self.timeout:
                        raise OllamaError(
                            f"{self.label} response exceeded size or time limit"
                        )
                    yield chunk
        except OllamaError:
            raise
        except (OSError, ValueError, HTTPError) as exc:
            if state["aborted"]:
                raise OllamaError(
                    "Model request stopped: the run was paused or cancelled"
                ) from None
            if isinstance(exc, HTTPError):
                raise OllamaError(
                    f"{self.label} connection failed; check the configured local service"
                ) from None
            raise OllamaError(f"{self.label} connection failed") from exc
        finally:
            _unregister(owner, state)

    def request(self, method, path, payload):
        chunks = self._chunks(method, path, payload)
        try:
            value = json.loads(b"".join(chunks))
        except (ValueError, UnicodeError):
            raise OllamaError(f"{self.label} returned invalid JSON") from None
        finally:
            chunks.close()
        if not isinstance(value, dict):
            raise OllamaError(f"{self.label} returned an invalid envelope")
        return value

    def stream(self, method, path, payload):
        pending = b""
        chunks = self._chunks(method, path, payload)
        try:
            for chunk in chunks:
                pending += chunk
                if len(pending) > 1024 * 1024:
                    raise OllamaError(f"{self.label} progress frame is too large")
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line.strip():
                        yield json.loads(line)
            if pending.strip():
                yield json.loads(pending)
        except (ValueError, UnicodeError):
            raise OllamaError(f"{self.label} returned invalid progress JSON") from None
        finally:
            chunks.close()


class OpenAILocalTransport(OllamaTransport):
    """The same pinned loopback transport for a local OpenAI-compatible server
    (vLLM, SGLang, llama.cpp's server): DNS pinning, no redirects or proxies,
    size and time limits, and pause/cancel cutting a request in flight. A key
    is sent only when the profile's variable holds one (``vllm --api-key``)."""

    label = "Local model server"
    paths = frozenset({"/v1/models", "/v1/chat/completions", "/version"})

    def __init__(
        self, config, *, timeout=120, max_response_bytes=16 * 1024 * 1024, key=None
    ):
        super().__init__(config, timeout=timeout, max_response_bytes=max_response_bytes)
        if key:
            self.headers = {"Authorization": f"Bearer {key}"}


_SMALLER = (
    "send fewer or smaller images (max_images, image_max_side) or serve a "
    "longer --max-model-len"
)
# (pattern, what LEVI says, from the match's groups)
_CONTEXT = (
    # Ollama
    (
        r"request \((\d+) tokens\) exceeds the available context size \((\d+) tokens\)",
        (
            "the request needs {0} tokens but the model context holds {1}; send "
            "fewer images or raise context_tokens"
        ),
    ),
    # vLLM refuses a prompt and answer allowance that together exceed its
    # context, the prompt alone may fit: 0.30's wording, then earlier ones.
    (
        (
            r"maximum context length is (\d+) tokens\. However, you requested "
            r"(\d+) output tokens and your prompt contains (?:at least )?(\d+) "
            r"input tokens"
        ),
        (
            "the prompt ({2} tokens) and the answer allowance ({1}) exceed the "
            "model context of {0} tokens; " + _SMALLER
        ),
    ),
    (
        (
            r"maximum context length is (\d+) tokens\. However, you requested "
            r"\d+ tokens \((\d+) in the messages, (\d+) in the completion\)"
        ),
        (
            "the prompt ({1} tokens) and the answer allowance ({2}) exceed the "
            "model context of {0} tokens; " + _SMALLER
        ),
    ),
    # ...and once the images are expanded into tokens
    (
        r"prompt \(length (\d+)\).*?longer than the maximum model length of (\d+)",
        "the request needs {0} tokens but the model context holds {1}; " + _SMALLER,
    ),
)


def _known_cause(response):
    """Only recognised, LEVI-rephrased causes: remote bodies are never echoed."""
    import re

    from httpx import HTTPError

    # Only the head is read: an error body is held to the same bound as an
    # answer, however large the server makes it.
    body = b""
    try:
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) >= 4096:
                break
    except (HTTPError, OSError):
        return ""
    body = body[:4096].decode("utf-8", "replace")
    found = re.search(
        r"maximum context length is (\d+) tokens.*?prompt contains (\d+) characters",
        body,
        re.DOTALL,
    )
    if found:
        return (
            f": the prompt has {found[2]} characters, more than the model context "
            f"of {found[1]} tokens can hold; {_SMALLER}"
        )
    for pattern, message in _CONTEXT:
        found = re.search(pattern, body, re.DOTALL)
        if found:
            return ": " + message.format(*found.groups())
    found = re.search(r"At most (\d+) image", body, re.IGNORECASE)
    if found:
        return (
            f": the server accepts at most {found[1]} images per request; set "
            f"max_images to {found[1]} or raise its --limit-mm-per-prompt"
        )
    if re.search(r"roles must alternate", body, re.IGNORECASE):
        return (
            ": the model's chat template refuses a system turn; enable "
            "fold_system on this profile"
        )
    if re.search(
        r"xgrammar|guidance|outlines|grammar|json.?schema", body, re.IGNORECASE
    ):
        return (
            ": the server's structured-output backend rejected the answer "
            "schema (vLLM: serve with --structured-outputs-config.backend=guidance)"
        )
    return ""
