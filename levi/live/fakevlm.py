"""A stand-in for the vLLM server, for tests and for trying the service
without a GPU (``levi live once --fake-vlm``).

It speaks the small slice of the OpenAI protocol LEVI's local path uses --
``GET /health``, ``/version``, ``/v1/models`` and ``POST /v1/chat/completions``
-- and answers every request with a *valid* answer built from the request's
own response schema:

- a temporal request (an object with ``proposals``): consecutive intervals
  over the episode named in the prompt ("Episode N: a s to b s"), one per
  subtask id it lists, each ``success``; a refinement (``prefixItems``) gets
  the draft's intervals back;
- any other schema (an anchored-review question): every field set to the
  value ``answers`` names for it, else the first enum value that is not
  ``unclear``.

It understands nothing about images. It exists to exercise the real
transport, the real plans and the real commit path.

Standard library only.
"""

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "qwen3.8-27b"
ROOT = "/fake/Qwen3.8-27B-INT4/snapshots/0000"
EPISODE = re.compile(r"Episode (\d+): ([\d.]+) s to ([\d.]+) s")
IDS = re.compile(r"Subtask ids: ([^\n]+?)\.\n")
DRAFT = re.compile(r"^\d+\. (\S+) ([\d.]+)-([\d.?]+) s ?(\S*)", re.MULTILINE)
SPECIAL = {"unknown", "other", "background"}


class Fake:
    def __init__(self, answers=None, max_model_len=49152, delay=0.0, fail=None):
        self.answers = dict(answers or {})
        self.max_model_len = max_model_len
        self.delay = delay
        # fail(n, request) -> True answers a server error (n counts chats).
        self.fail = fail
        self.calls = []
        self.lock = threading.Lock()
        # vLLM's dev endpoints (--enable-sleep-mode): a sleeping server
        # refuses to generate.
        self.sleeping = False
        self.health_checks = 0
        self.sleeps = 0
        self.wakes = 0
        # Requests started and finished (a test sees one in flight, and that
        # a cancelled one did not complete).
        self.started = 0
        self.finished = 0

    def served(self):
        return {
            "id": MODEL,
            "object": "model",
            "owned_by": "fake-vllm",
            "root": ROOT,
            "max_model_len": self.max_model_len,
        }

    def chat(self, payload):
        with self.lock:
            self.calls.append(payload)
            n = len(self.calls)
            self.started += 1
        if self.sleeping:
            raise RuntimeError("the server is asleep")
        if self.delay:
            time.sleep(self.delay)
        if self.fail and self.fail(n, payload):
            raise RuntimeError("injected failure")
        try:
            return self._chat(payload)
        finally:
            with self.lock:
                self.finished += 1

    def _chat(self, payload):
        if payload.get("max_completion_tokens") == 1:
            return self._reply("{", payload)
        schema = payload["response_format"]["json_schema"]["schema"]
        text = self._text(payload)
        if "proposals" in schema.get("properties", {}):
            content = self._temporal(schema, text)
        else:
            content = self._fields(schema)
        return self._reply(json.dumps(content), payload)

    @staticmethod
    def _text(payload):
        parts = payload["messages"][-1]["content"]
        if isinstance(parts, str):
            return parts
        return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")

    def _fields(self, schema):
        out = {}
        for name, spec in schema["properties"].items():
            choices = spec.get("enum") or []
            if name in self.answers:
                out[name] = self.answers[name]
            else:
                out[name] = next(
                    (c for c in choices if c != "unclear"),
                    choices[0] if choices else None,
                )
        return out

    def _temporal(self, schema, text):
        match = EPISODE.search(text)
        start, end = (
            (float(match.group(2)), float(match.group(3))) if match else (0.0, 4.0)
        )
        prefix = schema["properties"]["proposals"].get("prefixItems")
        draft = (
            DRAFT.findall(text.split("## Draft to refine")[1])
            if "## Draft to refine" in text
            else []
        )
        if prefix is not None:
            items = []
            for _sid, a, b, outcome in draft[: len(prefix)]:
                items.append(
                    {
                        "start": float(a),
                        "end": end if b == "?" else float(b),
                        "outcome": outcome
                        if outcome in ("success", "failure", "unknown")
                        else "unknown",
                    }
                )
            return {"proposals": items, "summary": "refined", "warnings": []}
        ids = IDS.search(text)
        subtasks = (
            [s.strip() for s in ids.group(1).split(",")]
            if ids
            else ["approach", "grasp"]
        )
        subtasks = [s for s in subtasks if s not in SPECIAL] or ["approach"]
        width = (end - start) / len(subtasks)
        items = []
        for i, sid in enumerate(subtasks):
            a = round(start + i * width, 3)
            b = end if i == len(subtasks) - 1 else round(start + (i + 1) * width, 3)
            items.append(
                {
                    "subtask_id": sid,
                    "start": a,
                    "end": b,
                    "outcome": "success",
                    "content": sid,
                }
            )
        return {"proposals": items, "summary": "fake annotation", "warnings": []}

    @staticmethod
    def _reply(content, payload):
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 400,
                "completion_tokens": max(1, len(content) // 4),
            },
        }


def handler(fake):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, code, value):
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                fake.health_checks += 1
                self._send(200, {})
            elif self.path == "/version":
                self._send(200, {"version": "0.0.0-fake"})
            elif self.path == "/v1/models":
                self._send(200, {"object": "list", "data": [fake.served()]})
            elif self.path == "/is_sleeping":
                self._send(200, {"is_sleeping": fake.sleeping})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path.startswith("/sleep"):
                fake.sleeping = True
                fake.sleeps += 1
                self._send(200, {})
                return
            if self.path == "/wake_up":
                fake.sleeping = False
                fake.wakes += 1
                self._send(200, {})
                return
            if self.path != "/v1/chat/completions":
                self._send(404, {"error": "not found"})
                return
            try:
                self._send(200, fake.chat(payload))
            except Exception:  # noqa: BLE001
                self._send(500, {"error": "fake failure"})

    return Handler


def serve(port=0, fake=None, host="127.0.0.1"):
    """Start a fake server in a thread; returns (server, fake, port)."""
    fake = fake or Fake()
    server = ThreadingHTTPServer((host, port), handler(fake))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="fake-vllm").start()
    return server, fake, server.server_address[1]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m levi.live.fakevlm")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--max-model-len", type=int, default=49152)
    args = parser.parse_args(argv)
    server, _fake, port = serve(args.port, Fake(max_model_len=args.max_model_len))
    print(f"fake vLLM on 127.0.0.1:{port}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
