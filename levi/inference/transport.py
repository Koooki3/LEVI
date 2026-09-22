"""Pinned, bounded loopback transport for a user-authorized Ollama endpoint."""

import ipaddress
import json
import time
from urllib.parse import urlsplit

from levi.agent.security import endpoint_addresses

from .ollama import OllamaError


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
            timeout=min(30, self.timeout),
        )

    def _chunks(self, method, path, payload):
        from httpx import HTTPError

        if path not in self.paths or method not in {"GET", "POST"}:
            raise ValueError("Unsupported Ollama operation")
        started = time.monotonic()
        total = 0
        try:
            with (
                self._client() as client,
                client.stream(method, self.url + path, json=payload) as response,
            ):
                if response.status_code != 200:
                    raise OllamaError(
                        f"Ollama HTTP request failed ({response.status_code})"
                    )
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self.limit or time.monotonic() - started > self.timeout:
                        raise OllamaError("Ollama response exceeded size or time limit")
                    yield chunk
        except OllamaError:
            raise
        except (OSError, ValueError) as exc:
            raise OllamaError("Ollama connection failed") from exc
        except HTTPError:
            raise OllamaError(
                "Ollama connection failed; check the configured local service"
            ) from None

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
