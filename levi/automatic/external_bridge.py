"""Loopback OpenAI-compatible file queue for independently operated external agents.

No model, GPU, robot client, subprocess or automatic label writer is involved.
Run with ``python -m levi.automatic.external_bridge serve --help``.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import copy
import io
import json
import math
import os
import stat
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from levi.live.jsonio import write as write_json

from . import external_report

PROTECTED_PORTS = frozenset({5000, 5001, 5100, 7470, 8000})
IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def strict_json(raw):
    """Reject duplicate keys, nonfinite numbers and excessive JSON nesting."""

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
        # Also rejects overflowing exponents (1e400), unlike parse_constant alone.
        json.dumps(value, allow_nan=False)
        return value
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("invalid UTF-8 JSON or excessive nesting") from exc


def prepare_request(body):
    """Validate structure and extract inline images without fetching any URLs."""
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise TypeError("messages must be a list")
    if body.get("stream", False) is not False:
        raise ValueError("streaming is not supported")
    slim = copy.deepcopy(body)
    texts, images = [], {}
    for message in slim["messages"]:
        if not isinstance(message, dict):
            raise TypeError("message must be an object")
        content = message.get("content")
        if isinstance(content, str):
            texts.append(f"[{message.get('role')}] {content}")
            continue
        if not isinstance(content, list):
            raise TypeError("message content must be text or a list")
        for part in content:
            if not isinstance(part, dict):
                raise TypeError("content part must be an object")
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(f"[{message.get('role')}] {part['text']}")
            elif part.get("type") == "image_url":
                image = part.get("image_url")
                url = image.get("url") if isinstance(image, dict) else None
                if not isinstance(url, str):
                    raise ValueError("image_url.url must be a data:image URL")
                header, separator, encoded = url.partition(",")
                media = header.removeprefix("data:").removesuffix(";base64")
                if (
                    not separator
                    or header != f"data:{media};base64"
                    or media not in IMAGE_TYPES
                ):
                    raise ValueError(
                        "only base64 data:image/jpeg, png or webp are supported"
                    )
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise ValueError("invalid image base64") from exc
                if not data:
                    raise ValueError("empty image")
                filename = f"img_{len(images) + 1}.{IMAGE_TYPES[media]}"
                images[filename] = data
                image["url"] = filename
            else:
                raise ValueError("unsupported content part")
    return slim, "\n\n".join(texts), images


def publish_request(queue: Path, body: dict) -> Path:
    """READY is the publication boundary; consumers MUST ignore other folders."""
    slim, prompt, images = prepare_request(body)
    queue.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = Path(tempfile.mkdtemp(prefix="request-", dir=queue))
    for filename, data in images.items():
        (folder / filename).write_bytes(data)
    write_json(folder / "request.json", slim, indent=1)
    write_json(folder / "schema.json", body.get("response_format"), indent=1)
    (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
    (folder / "BRIEF.md").write_text(
        "# LEVI external request brief v1\n\n"
        f"Request ID: `{folder.name}`. Work only on this request directory.\n\n"
        "1. Proceed only if `READY` exists and `result.json` does not. "
        "Read `request.json` for authoritative message roles, ordering and image references; "
        "`prompt.txt` is a readable text projection.\n"
        "2. Read `schema.json` for the requested response format. "
        "Inspect the image files referenced by this request in their message order. "
        "Use the actual task instructions; this brief adds no success criteria.\n"
        "3. Publish one answer: write the assistant content as strict UTF-8 JSON "
        "to a temporary file here, close it, then atomically rename it to `answer.json`. "
        "Do not use a completion envelope, Markdown fences, duplicate keys or non-finite numbers. "
        "Never overwrite a published answer.\n"
        "4. Do not modify request files, read another request or external session, "
        "start services, contact robots, or write operator labels. "
        "The answer is a provider judgement, not an operator label. "
        "A result marker means this request is no longer awaiting an answer; "
        "late answers cannot complete another request.\n",
        encoding="utf-8",
    )
    # Atomic helper publishes this last, after every request file is closed.
    write_json(folder / "READY", {"protocol": "levi.external-request.v1"})
    return folder


def read_answer(path: Path, max_bytes: int) -> str:
    """Read a bounded regular file; never follow answer symlinks or block on FIFOs."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError("answer must be a bounded regular file")
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("answer too large")
    try:
        content = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("answer must be UTF-8") from exc
    strict_json(content)
    return content


def wait_for_answer(
    folder: Path,
    timeout_s: float,
    max_bytes: int,
    *,
    clock=time.monotonic,
    sleep=time.sleep,
    before_read=None,
) -> str:
    """Only missing files are retried; a published invalid answer fails closed."""
    deadline = clock() + timeout_s
    while clock() < deadline:
        if before_read is not None:
            before_read()
        try:
            content = read_answer(folder / "answer.json", max_bytes)
            if clock() >= deadline:
                raise TimeoutError("answer arrived after deadline")
            return content
        except FileNotFoundError:
            sleep(min(0.2, max(0.0, deadline - clock())))
    raise TimeoutError("external agent did not answer before deadline")


def report_snapshot(folder: Path):
    """Only this fixed file, once per poll before checking for an answer.

    No waiting for metadata, no following symlinks/FIFOs and no late rereads.
    """
    try:
        fd = os.open(
            folder / "report.json", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
        )
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > external_report.MAX_REPORT_BYTES
            ):
                return "invalid", None
            raw = handle.read(external_report.MAX_REPORT_BYTES + 1)
        if len(raw) > external_report.MAX_REPORT_BYTES:
            return "invalid", None
        return "candidate", raw
    except FileNotFoundError:
        return "unavailable", None
    except OSError:
        return "invalid", None


def completion(folder: Path, content: str, model: str) -> dict:
    # External billing belongs in a separately reported record, never fake zeros.
    return {
        "id": folder.name,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }


class Bridge:
    def __init__(self, args, *, clock=time.monotonic, sleep=time.sleep):
        self.args = args
        self.clock = clock
        self.sleep = sleep
        self.sleeping = False
        self.lock = threading.Lock()
        self.pending = threading.BoundedSemaphore(args.max_pending)

    def set_sleeping(self, value):
        with self.lock:
            self.sleeping = value

    def is_sleeping(self):
        with self.lock:
            return self.sleeping

    def answer(self, body):
        with self.lock:
            if self.sleeping:
                return 503, {"error": "asleep"}
            if not self.pending.acquire(blocking=False):
                return 429, {"error": "external request capacity reached"}
        folder = None
        try:
            folder = publish_request(self.args.queue, body)
            began = self.clock()
            snapshot = ("unavailable", None)

            def capture_report():
                nonlocal snapshot
                snapshot = report_snapshot(folder)

            content = wait_for_answer(
                folder,
                self.args.answer_timeout_s,
                self.args.max_answer_bytes,
                clock=self.clock,
                sleep=self.sleep,
                before_read=capture_report,
            )
            wait_ms = max(0, int((self.clock() - began) * 1000))
            metadata = external_report.evidence(snapshot, folder.name, content, wait_ms)
            write_json(
                folder / "result.json",
                {"status": "answer_ready", "consumption": "unknown"},
            )
            response = completion(folder, content, self.args.served_model_name)
            response["levi_external"] = metadata
            return 200, response
        except TimeoutError:
            if folder is not None:
                with contextlib.suppress(OSError):
                    write_json(folder / "result.json", {"status": "timeout"})
            return 504, {"error": "external agent did not answer before deadline"}
        except (OSError, ValueError, TypeError, RecursionError):
            if folder is not None:
                with contextlib.suppress(OSError):
                    write_json(folder / "result.json", {"status": "invalid_answer"})
                return 502, {"error": "invalid or unreadable external answer"}
            return 400, {"error": "invalid or unwritable request"}
        finally:
            self.pending.release()


class DeadlineReader(io.RawIOBase):
    """Apply one absolute deadline to every underlying request-line/header recv."""

    def __init__(self, connection, timeout_s, clock):
        self.connection = connection
        self.clock = clock
        self.deadline = clock() + timeout_s
        self.timeout_s = timeout_s

    def readable(self):
        return True

    def readinto(self, buffer):
        budget = (
            self.timeout_s if self.deadline is None else self.deadline - self.clock()
        )
        if budget <= 0:
            raise TimeoutError("request line/header deadline exceeded")
        self.connection.settimeout(budget)
        count = self.connection.recv_into(buffer)
        if self.deadline is not None and self.clock() >= self.deadline:
            raise TimeoutError("request line/header deadline exceeded")
        return count


def handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.rfile.close()
            self.header_reader = DeadlineReader(
                self.connection, bridge.args.read_timeout_s, bridge.clock
            )
            self.rfile = io.BufferedReader(self.header_reader)

        def parse_request(self):
            try:
                return super().parse_request()
            finally:
                self.header_reader.deadline = None

        def log_message(self, *args):
            pass

        def send_json(self, status, value):
            data = json.dumps(value, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                self.send_json(200, {"ok": True})
            elif path == "/is_sleeping":
                self.send_json(200, {"is_sleeping": bridge.is_sleeping()})
            elif path == "/v1/models":
                self.send_json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {"id": bridge.args.served_model_name, "object": "model"}
                        ],
                    },
                )
            else:
                self.send_json(404, {"error": "not found"})

        def do_POST(self):
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if self.headers.get("Transfer-Encoding") or len(lengths) > 1:
                    raise ValueError("ambiguous framing")
                length = int(lengths[0]) if lengths else 0
                if not 0 <= length <= bridge.args.max_request_bytes:
                    self.send_json(413, {"error": "request too large"})
                    return
                deadline = bridge.clock() + bridge.args.read_timeout_s
                if hasattr(self, "header_reader"):
                    self.header_reader.deadline = deadline
                chunks, remaining = [], length
                while remaining:
                    budget = deadline - bridge.clock()
                    if budget <= 0:
                        raise TimeoutError("body deadline exceeded")
                    self.connection.settimeout(budget)
                    chunk = self.rfile.read1(min(remaining, 65536))
                    if not chunk:
                        raise ValueError("incomplete body")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
                path = urlsplit(self.path).path
                if path in ("/sleep", "/wake_up"):
                    bridge.set_sleeping(path == "/sleep")
                    self.send_json(200, {"ok": True})
                elif path == "/v1/chat/completions":
                    self.send_json(*bridge.answer(strict_json(raw.decode("utf-8"))))
                else:
                    self.send_json(404, {"error": "not found"})
            except (ValueError, UnicodeError, RecursionError):
                self.send_json(400, {"error": "invalid request"})
            except TimeoutError:
                self.send_json(408, {"error": "request read timeout"})

    return Handler


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("model")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--served-model-name", required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--answer-timeout-s", type=float, default=30.0)
    parser.add_argument("--read-timeout-s", type=float, default=10.0)
    parser.add_argument(
        "--request-timeout-s",
        type=float,
        default=60.0,
        help="must match the caller job plan.subtask.vlm.request_timeout_s",
    )
    parser.add_argument("--max-request-bytes", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--max-answer-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--max-pending", type=int, default=1)
    args, ignored = parser.parse_known_args(argv)
    if args.host not in ("127.0.0.1", "localhost"):
        parser.error("host must be 127.0.0.1 or localhost")
    # Pin the actual bind address; never resolve localhost through DNS.
    args.host = "127.0.0.1"
    if args.port in PROTECTED_PORTS or not 1024 <= args.port <= 65535:
        parser.error("port must be unprivileged and not a protected robot/policy port")
    for name, maximum in (
        ("answer_timeout_s", 3600),
        ("request_timeout_s", 7200),
        ("read_timeout_s", 60),
        ("max_request_bytes", 128 * 1024 * 1024),
        ("max_answer_bytes", 8 * 1024 * 1024),
        ("max_pending", 16),
    ):
        value = getattr(args, name)
        if not math.isfinite(value) or not 0 < value <= maximum:
            parser.error(f"{name} must be positive, finite and at most {maximum}")
    if args.answer_timeout_s + 2 * args.read_timeout_s >= args.request_timeout_s:
        parser.error(
            "answer-timeout-s + 2 * read-timeout-s must be less than "
            "request-timeout-s (the caller job plan.subtask.vlm.request_timeout_s)"
        )
    return args, ignored


class BridgeServer(ThreadingHTTPServer):
    """Bound connection workers, including clients that never finish their headers."""

    daemon_threads = True

    def __init__(self, address, request_handler):
        self.connections = threading.BoundedSemaphore(32)
        super().__init__(address, request_handler)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()


def main(argv=None):
    args, ignored = parse_args(argv)
    if ignored:
        # Avoid printing arbitrary argv values (which could contain credentials).
        print(
            "external_bridge: ignoring unrecognized vLLM compatibility arguments",
            file=sys.stderr,
        )
    server = BridgeServer((args.host, args.port), handler(Bridge(args)))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
