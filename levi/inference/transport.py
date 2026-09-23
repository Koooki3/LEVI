"""Pinned, bounded loopback transport for a user-authorized Ollama endpoint."""

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
# as long as the model generates, and Ollama stops generating when the client
# goes away, so a run's requests carry its id (``requests_of``) and ``abort``
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


def validate_ollama_endpoint(url: str, allow_localhost: bool) -> list[str]:
    parsed = urlsplit(url)
    if not allow_localhost or parsed.path not in {"", "/"}:
        raise ValueError("Ollama requires an explicitly allowed loopback service root")
    addresses = endpoint_addresses(url, allow_localhost=True)
    if not all(ipaddress.ip_address(address).is_loopback for address in addresses):
        raise ValueError("This Ollama adapter supports loopback services only")
    return sorted(addresses)


class OllamaTransport:
    """No redirects, environment proxies, automatic retries or implicit pulls.

    The client is created per request, closes on cancellation, pins DNS, and
    limits response bytes as well as total duration. Download bodies consist of
    progress only; model blobs stay inside the Ollama service's model directory.
    """

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

    def _client(self):
        import httpx

        addresses = validate_ollama_endpoint(self.url, self.allow_localhost)
        parsed = urlsplit(self.url)
        address = addresses[0]

        class Pinned(httpx.HTTPTransport):
            def handle_request(self, request):
                if request.url.host != parsed.hostname:
                    raise ValueError("Unapproved Ollama destination")
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
            raise ValueError("Unsupported Ollama operation")
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
                    extensions={"trace": _register(owner, state)},
                ) as response,
            ):
                if response.status_code != 200:
                    raise OllamaError(
                        f"Ollama HTTP request failed ({response.status_code})"
                        + _known_cause(response)
                    )
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self.limit or time.monotonic() - started > self.timeout:
                        raise OllamaError("Ollama response exceeded size or time limit")
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
                    "Ollama connection failed; check the configured local service"
                ) from None
            raise OllamaError("Ollama connection failed") from exc
        finally:
            _unregister(owner, state)

    def request(self, method, path, payload):
        chunks = self._chunks(method, path, payload)
        try:
            value = json.loads(b"".join(chunks))
        except (ValueError, UnicodeError):
            raise OllamaError("Ollama returned invalid JSON") from None
        finally:
            chunks.close()
        if not isinstance(value, dict):
            raise OllamaError("Ollama returned an invalid envelope")
        return value

    def stream(self, method, path, payload):
        pending = b""
        chunks = self._chunks(method, path, payload)
        try:
            for chunk in chunks:
                pending += chunk
                if len(pending) > 1024 * 1024:
                    raise OllamaError("Ollama progress frame is too large")
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line.strip():
                        yield json.loads(line)
            if pending.strip():
                yield json.loads(pending)
        except (ValueError, UnicodeError):
            raise OllamaError("Ollama returned invalid progress JSON") from None
        finally:
            chunks.close()


def _known_cause(response):
    """Only recognised, LEVI-rephrased causes: remote bodies are never echoed."""
    import re

    from httpx import HTTPError

    try:
        body = response.read()[:4096].decode("utf-8", "replace")
    except (HTTPError, OSError):
        return ""
    found = re.search(
        r"request \((\d+) tokens\) exceeds the available context size \((\d+) tokens\)",
        body,
    )
    if found:
        return (
            f": the request needs {found[1]} tokens but the model context holds "
            f"{found[2]}; send fewer images or raise context_tokens"
        )
    return ""
