"""File queue and protocol tests: no listening sockets, GPU or robot connection."""

import io
import json
from types import SimpleNamespace

import pytest

from levi.automatic import external_bridge as bridge


def args(tmp_path, *extra):
    return bridge.parse_args(
        [
            "serve",
            "frozen-model",
            "--port",
            "18991",
            "--served-model-name",
            "external",
            "--queue",
            str(tmp_path),
            *extra,
        ]
    )[0]


def body():
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "判断"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,YWJj"},
                    },
                ],
            }
        ],
        "response_format": {"type": "json_schema", "json_schema": {"name": "test"}},
    }


def test_publish_complete_unique_and_preserve_input(tmp_path, monkeypatch):
    original = body()
    writes = bridge.write_json
    observed = []

    def inspect(path, value, **kwargs):
        if path.name == "READY":
            assert not path.exists()
            assert (path.parent / "img_1.png").read_bytes() == b"abc"
            assert (path.parent / "prompt.txt").read_text() == "[user] 判断"
            assert json.loads((path.parent / "schema.json").read_text())
            brief = (path.parent / "BRIEF.md").read_text()
            assert path.parent.name in brief and "request.json" in brief
            assert "schema.json" in brief and "answer.json" in brief
            observed.append(path.parent)
        writes(path, value, **kwargs)

    monkeypatch.setattr(bridge, "write_json", inspect)
    first = bridge.publish_request(tmp_path, original)
    (first / "answer.json").write_text('{"old": true}')
    second = bridge.publish_request(tmp_path, original)
    assert first != second and observed == [first, second]
    assert (first / "answer.json").read_text() == '{"old": true}'
    assert original == body()
    assert "data:image" not in (first / "request.json").read_text()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/x.png",
        "file:///tmp/x",
        "data:text/plain;base64,YQ==",
        "data:image/png;base64,%%%",
        "data:image/png;base64,",
    ],
)
def test_reject_non_inline_or_invalid_images(tmp_path, url):
    value = body()
    value["messages"][0]["content"][1]["image_url"]["url"] = url
    with pytest.raises(ValueError):
        bridge.publish_request(tmp_path, value)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "value",
    [
        [],
        {},
        {"messages": [None]},
        {"messages": [{"content": None}]},
        {"messages": [], "stream": True},
    ],
)
def test_reject_invalid_requests(value):
    with pytest.raises((ValueError, TypeError)):
        bridge.prepare_request(value)


@pytest.mark.parametrize("raw", [b"bad", b"\xff", b'{"x":1,"x":2}', b"NaN", b"1e400"])
def test_invalid_answer(tmp_path, raw):
    target = tmp_path / "answer.json"
    target.write_bytes(raw)
    with pytest.raises(ValueError):
        bridge.read_answer(target, 1024)


def test_answer_bound_regular_file_and_unicode(tmp_path):
    target = tmp_path / "answer.json"
    target.write_text('{"text":"判断"}', encoding="utf-8")
    assert "判断" in bridge.read_answer(target, 100)
    with pytest.raises(ValueError):
        bridge.read_answer(target, 4)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        bridge.read_answer(link, 100)
    import os

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        bridge.read_answer(fifo, 100)


def test_wait_deadline_and_delayed_answer(tmp_path):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(TimeoutError):
        bridge.wait_for_answer(tmp_path, 0.3, 100, clock=lambda: now[0], sleep=sleep)
    assert now[0] == pytest.approx(0.3)

    def deliver(seconds):
        sleep(seconds)
        (tmp_path / "answer.json").write_text('{"ok":true}')

    assert bridge.wait_for_answer(tmp_path, 1, 100, clock=lambda: now[0], sleep=deliver)


def test_bridge_sleep_capacity_success_and_usage(tmp_path, monkeypatch):
    state = bridge.Bridge(args(tmp_path))
    state.set_sleeping(True)
    assert state.answer(body())[0] == 503
    state.set_sleeping(False)
    assert state.pending.acquire(blocking=False)
    assert state.answer(body())[0] == 429
    state.pending.release()
    monkeypatch.setattr(bridge, "wait_for_answer", lambda *a, **kw: '{"ok":true}')
    status, response = state.answer(body())
    assert status == 200 and "usage" not in response
    assert response["choices"][0]["message"]["content"] == '{"ok":true}'
    assert (
        json.loads(next(tmp_path.glob("*/result.json")).read_text())["status"]
        == "answer_ready"
    )


@pytest.mark.parametrize("error,status", [(TimeoutError(), 504), (ValueError(), 502)])
def test_bridge_errors_release_capacity(tmp_path, monkeypatch, error, status):
    state = bridge.Bridge(args(tmp_path))

    def fail(*unused, **kwargs):
        raise error

    monkeypatch.setattr(bridge, "wait_for_answer", fail)
    assert state.answer(body())[0] == status
    assert state.pending.acquire(blocking=False)


@pytest.mark.parametrize(
    "extra",
    [
        ["--host", "0.0.0.0"],
        ["--port", "8000"],
        ["--port", "5000"],
        ["--port", "5001"],
        ["--port", "5100"],
        ["--port", "7470"],
        ["--answer-timeout-s", "nan"],
        ["--max-pending", "0"],
        ["--max-answer-bytes", "999999999"],
    ],
)
def test_cli_boundaries(tmp_path, extra):
    with pytest.raises(SystemExit):
        args(tmp_path, *extra)


def test_cli_vllm_compatibility_does_not_abbreviate_host(tmp_path):
    value = args(
        tmp_path,
        "--max-model-len",
        "4096",
        "--gpu-memory-utilization",
        ".6",
        "--hos",
        "0.0.0.0",
        "--host",
        "localhost",
    )
    assert value.host == "127.0.0.1" and value.port == 18991


def fake_handler(tmp_path, method, path, raw=b"", headers=None):
    state = bridge.Bridge(args(tmp_path))
    cls = bridge.handler(state)
    request = object.__new__(cls)
    request.path = path
    from email.message import Message

    request.headers = Message()
    for key, value in (headers or {"Content-Length": str(len(raw))}).items():
        request.headers[key] = value
    request.rfile = io.BufferedReader(io.BytesIO(raw))
    request.connection = SimpleNamespace(settimeout=lambda _: None)
    output = []
    request.send_json = lambda *a, **kw: output.append(a)
    getattr(request, method)()
    return output[0]


@pytest.mark.parametrize("path", ["/health", "/v1/models", "/is_sleeping"])
def test_get_contract_without_socket(tmp_path, path):
    assert fake_handler(tmp_path, "do_GET", path)[0] == 200


@pytest.mark.parametrize("path", ["/sleep?level=2", "/wake_up"])
def test_power_contract_without_socket(tmp_path, path):
    assert fake_handler(tmp_path, "do_POST", path) == (200, {"ok": True})


def test_http_body_framing_and_bounds(tmp_path):
    assert fake_handler(tmp_path, "do_POST", "/v1/chat/completions", b"{}")[0] == 400
    assert (
        fake_handler(
            tmp_path, "do_POST", "/sleep", headers={"Content-Length": "999999999"}
        )[0]
        == 413
    )
    assert (
        fake_handler(
            tmp_path, "do_POST", "/sleep", headers={"Transfer-Encoding": "chunked"}
        )[0]
        == 400
    )
    assert (
        fake_handler(tmp_path, "do_POST", "/sleep", headers={"Content-Length": "3"})[0]
        == 400
    )


class FakeSocket:
    """Exercise BaseHTTPRequestHandler.setup/handle/finish without a socket."""

    def __init__(self, data, now, *, drip=0.0, disconnect=False):
        self.source = io.BytesIO(data)
        self.output = bytearray()
        self.now = now
        self.drip = drip
        self.disconnect = disconnect

    def makefile(self, *args):
        return io.BytesIO()

    def settimeout(self, seconds):
        self.timeout = seconds

    def recv_into(self, buffer):
        self.now[0] += self.drip
        data = self.source.read(1 if self.drip else len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def sendall(self, data):
        if self.disconnect:
            raise BrokenPipeError("caller disconnected")
        self.output.extend(data)


def test_full_handle_header_drip_absolute_deadline(tmp_path):
    now = [0.0]
    state = bridge.Bridge(args(tmp_path), clock=lambda: now[0])
    peer = FakeSocket(b"GET /health HTTP/1.1\r\nX-Slow: " + b"a" * 1000, now, drip=0.1)
    bridge.handler(state)(peer, ("127.0.0.1", 1), SimpleNamespace())
    assert now[0] <= state.args.read_timeout_s + 0.1
    assert not peer.output and not list(tmp_path.iterdir())
    healthy = FakeSocket(b"GET /health HTTP/1.1\r\n\r\n", now)
    bridge.handler(state)(healthy, ("127.0.0.1", 1), SimpleNamespace())
    assert b"200 OK" in healthy.output


def test_full_handle_request_line_drip_deadline(tmp_path):
    now = [0.0]
    state = bridge.Bridge(args(tmp_path), clock=lambda: now[0])
    peer = FakeSocket(b"GET /" + b"a" * 1000, now, drip=0.1)
    bridge.handler(state)(peer, ("127.0.0.1", 1), SimpleNamespace())
    assert now[0] <= state.args.read_timeout_s + 0.1
    assert not peer.output


def test_caller_budget_timeout_releases_slot_before_caller_deadline(tmp_path):
    config = args(tmp_path)
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    state = bridge.Bridge(config, clock=lambda: now[0], sleep=sleep)
    assert state.answer(body())[0] == 504
    assert now[0] == config.answer_timeout_s < config.request_timeout_s
    folder = next(tmp_path.glob("request-*"))
    (folder / "answer.json").write_text('{"late":true}')
    assert json.loads((folder / "result.json").read_text())["status"] == "timeout"
    assert state.pending.acquire(blocking=False)


def test_explicit_long_budget_requires_matching_caller_budget(tmp_path):
    with pytest.raises(SystemExit):
        args(tmp_path, "--answer-timeout-s", "540")
    config = args(tmp_path, "--answer-timeout-s", "540", "--request-timeout-s", "600")
    assert (
        config.answer_timeout_s + 2 * config.read_timeout_s < config.request_timeout_s
    )


def test_answer_read_crossing_deadline_is_not_accepted(tmp_path, monkeypatch):
    now = [0.0]

    def read(*unused):
        now[0] = 2.0
        return "{}"

    monkeypatch.setattr(bridge, "read_answer", read)
    with pytest.raises(TimeoutError):
        bridge.wait_for_answer(tmp_path, 1.0, 100, clock=lambda: now[0])


def test_disconnected_caller_never_recorded_as_consumed(tmp_path):
    now = [0.0]

    def deliver(seconds):
        now[0] += seconds
        next(tmp_path.glob("request-*")).joinpath("answer.json").write_text("{}")

    state = bridge.Bridge(args(tmp_path), clock=lambda: now[0], sleep=deliver)
    raw = json.dumps(body()).encode()
    data = (
        b"POST /v1/chat/completions HTTP/1.1\r\nContent-Length: "
        + str(len(raw)).encode()
        + b"\r\n\r\n"
        + raw
    )
    peer = FakeSocket(data, now, disconnect=True)
    with pytest.raises(BrokenPipeError):
        bridge.handler(state)(peer, ("127.0.0.1", 1), SimpleNamespace())
    record = json.loads(next(tmp_path.glob("*/result.json")).read_text())
    assert record == {"status": "answer_ready", "consumption": "unknown"}


def test_full_http_completion_times_out_inside_caller_budget(tmp_path):
    now = [0.0]

    def advance(seconds):
        now[0] += seconds

    state = bridge.Bridge(args(tmp_path), clock=lambda: now[0], sleep=advance)
    raw = json.dumps(body()).encode()
    data = (
        b"POST /v1/chat/completions HTTP/1.1\r\nContent-Length: "
        + str(len(raw)).encode()
        + b"\r\n\r\n"
        + raw
    )
    peer = FakeSocket(data, now)
    bridge.handler(state)(peer, ("127.0.0.1", 1), SimpleNamespace())
    assert b"504 Gateway Timeout" in peer.output
    assert now[0] < state.args.request_timeout_s
    assert state.pending.acquire(blocking=False)


def test_report_snapshot_is_optional_and_cannot_invalidate_answer(tmp_path):
    import hashlib

    from levi.automatic import external_report

    now = [0.0]
    content = '{ "ok": true }\r\n'

    def deliver(seconds):
        now[0] += seconds
        folder = next(tmp_path.glob("request-*"))
        bridge.write_json(
            folder / "report.json",
            {
                "schema": external_report.REPORT_SCHEMA,
                "request_id": folder.name,
                "answer_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "agent_notes": "Visible observation only",
                "elapsed_ms": 123,
                "tokens": {"input_new": 0, "output": 4},
            },
        )
        (folder / "answer.json").write_bytes(content.encode())

    state = bridge.Bridge(args(tmp_path), clock=lambda: now[0], sleep=deliver)
    status, response = state.answer(body())
    assert status == 200 and "usage" not in response
    assert response["choices"][0]["message"]["content"] == content
    meta = response["levi_external"]
    assert meta["report_status"] == "reported"
    assert meta["report"]["tokens"]["cache_read"] is None
    assert meta["bridge_wait_ms"] == 200 and meta["report"]["elapsed_ms"] == 123


@pytest.mark.parametrize(
    "report_kind", ["malformed", "oversized", "symlink", "fifo", "mismatch"]
)
def test_invalid_report_does_not_change_a_valid_answer(tmp_path, report_kind):
    import os

    now = [0.0]

    def deliver(seconds):
        now[0] += seconds
        folder = next(tmp_path.glob("request-*"))
        target = folder / "report.json"
        if report_kind == "symlink":
            target.symlink_to("answer.json")
        elif report_kind == "fifo":
            os.mkfifo(target)
        elif report_kind == "oversized":
            target.write_bytes(b"x" * (16 * 1024 + 1))
        elif report_kind == "mismatch":
            bridge.write_json(
                target,
                {
                    "schema": "levi.aeri.external_report.v1",
                    "request_id": "request-other",
                    "answer_sha256": "0" * 64,
                },
            )
        else:
            target.write_text("not JSON")
        (folder / "answer.json").write_text("{}")

    state = bridge.Bridge(args(tmp_path), clock=lambda: now[0], sleep=deliver)
    status, response = state.answer(body())
    assert status == 200 and response["choices"][0]["message"]["content"] == "{}"
    assert response["levi_external"]["report"] is None
    assert response["levi_external"]["report_status"] == (
        "mismatch" if report_kind == "mismatch" else "invalid"
    )


def test_report_after_answer_snapshot_is_never_backfilled(tmp_path, monkeypatch):
    import hashlib

    now = [0.0]

    def deliver(seconds):
        now[0] += seconds
        next(tmp_path.glob("request-*")).joinpath("answer.json").write_text("{}")

    original = bridge.read_answer

    def read_then_publish(path, maximum):
        content = original(path, maximum)
        bridge.write_json(
            path.parent / "report.json",
            {
                "schema": "levi.aeri.external_report.v1",
                "request_id": path.parent.name,
                "answer_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "agent_id": "late",
            },
        )
        return content

    monkeypatch.setattr(bridge, "read_answer", read_then_publish)
    state = bridge.Bridge(args(tmp_path), clock=lambda: now[0], sleep=deliver)
    status, response = state.answer(body())
    assert status == 200
    assert response["levi_external"]["report_status"] == "unavailable"
    assert response["levi_external"]["report"] is None
