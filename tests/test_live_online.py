"""The online judgement (interface C5): the endpoint, its strict request,
the same final-state rule as the background review, fast refusals, the log,
the status file, reading the relayed result back, and the background
labelling switched off. A protocol stand-in replaces the model; no GPU."""

import base64
import http.client
import json
import socket
import threading
import time
from pathlib import Path

import pytest
from conftest import scaled
from live_helpers import Rollouts
from test_live_gpu import Machine, live, serve, wait_for  # noqa: F401  (fixtures)

from levi.agent import anchored
from levi.live import api, controller, criteria, fakevlm, generic, mirror, online, stats
from levi.live import config as live_config

SPEC = "generic-final.v1.json"
TASK = "put the red cup on the plate"
JPEG = b"\xff\xd8\xff\xdb" + b"\x00" * 64 + b"\xff\xd9"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cfg(tmp_path, **online_over):
    c = live_config.Config()
    c.service.workspace = str(tmp_path / "ws")
    c.service.home = str(tmp_path / "home")
    c.watch.roots = [str(tmp_path / "rollouts")]
    c.watch.backlog = "process"
    c.watch.settle_s = 0.0
    c.gpu.mode = "manual"
    c.gpu.lock_file = ""
    c.online.enabled = True
    c.online.port = free_port()
    for key, value in online_over.items():
        setattr(c.online, key, value)
    return c.validate()


def views():
    raw = json.loads(generic.text(SPEC))
    return [(v["role"], v["offsets_seconds"]) for v in raw["views"]]


def images(skip=(), extra=()):
    out = []
    for role, offsets in views():
        for n, offset in enumerate(offsets):
            if (role, offset) in skip:
                continue
            out.append(
                {
                    "role": role,
                    "offset_s": offset,
                    "step": 100 + n,
                    "jpeg_b64": base64.b64encode(JPEG).decode(),
                }
            )
    return out + list(extra)


def request(**over):
    body = {
        "schema": online.REQUEST_SCHEMA,
        "task": TASK,
        "episode": {
            "group": "pi05_group_marker",
            "task_folder": "cups",
            "demo": "demo_0042",
            "run_id": "RUN-MARKER-7f3a",
            "steps": 120,
            "fps": 10.0,
        },
        "images": images(),
    }
    body.update(over)
    return body


class Gpu:
    """The supervisor as the judge sees it."""

    def __init__(self, port=None):
        self.admit = (True, None, None)
        self.check = (True, None, None)
        self.admitted = 0
        self.done = 0
        self.port = port

    def online_admit(self, now):
        if self.admit[0]:
            self.admitted += 1
        return self.admit

    def online_check(self, now):
        return self.check

    def online_done(self):
        self.done += 1

    def provider_spec(self):
        return {
            "name": "live-qwen38",
            "base_url": f"http://127.0.0.1:{self.port}",
            "port": self.port,
            "model": fakevlm.MODEL,
            "context_tokens": 49152,
            "max_images": 128,
        }


class Ask:
    """A model stand-in: answers ``answer`` (after ``hold`` seconds, or once
    ``release`` is set) and records what it was given."""

    def __init__(self, answer=None, hold=0.0):
        self.answer = answer or {
            "object_state": "resting_at_destination",
            "stable": "yes",
        }
        self.hold = hold
        self.release = threading.Event()
        self.calls = []

    def __call__(self, spec, folder, names):
        self.calls.append(
            {
                "question": spec.question,
                "names": list(names),
                "files": [(Path(folder) / n).read_bytes() for n in names],
            }
        )
        if self.hold:
            self.release.wait(self.hold)
        return json.dumps(self.answer), {
            "reported_tokens": 512,
            "prompt_tokens": 480,
        }


def judge_with(c, gpu=None, ask=None):
    return online.Judge(c, gpu or Gpu(), ask=ask or Ask())


def post(judge, body):
    return judge.handle(json.dumps(body).encode())


# --- configuration ----------------------------------------------------------------------


def test_online_is_off_by_default_on_loopback_port_7882_with_the_final_state_spec():
    c = live_config.Config()
    o = c.online
    assert (o.enabled, o.host, o.port, o.spec) == (
        False,
        "127.0.0.1",
        7882,
        "generic-final.v1.json",
    )
    assert (o.timeout_s, o.max_body_mb) == (15.0, 8.0)
    assert c.pipeline.background is True


@pytest.mark.parametrize(
    "toml,message",
    [
        ('[online]\nhost = "0.0.0.0"\n', "loopback"),
        ('[online]\nhost = "192.168.1.20"\n', "loopback"),
        ('[online]\nhost = "localhost"\n', "loopback"),
        ("[online]\nenabled = true\nport = 7861\n", "reserved"),
        ("[online]\nenabled = true\nport = 8000\n", "reserved"),
        ("[online]\nenabled = true\nport = 7881\n", "service.core_port"),
        ("[online]\nenabled = true\nport = 8100\n", "vllm.port"),
        ("[online]\ntimeout_s = 0.2\n", "timeout_s"),
        ("[online]\nmax_body_mb = 100\n", "max_body_mb"),
        (
            '[online]\nenabled = true\nspec = "generic-release.v3.json"\n',
            "not a final-state spec",
        ),
        ('[online]\nenabled = true\nspec = "../config.py"\n', "cannot be read"),
        ("[online]\nprompt = 'x'\n", "Unknown key"),
    ],
)
def test_a_wrong_online_setting_is_an_error(tmp_path, toml, message):
    path = tmp_path / "live.toml"
    path.write_text(toml)
    with pytest.raises(ValueError, match=message):
        live_config.load(path)


def test_the_rendered_file_holds_online_and_background_and_reads_back(tmp_path):
    c = live_config.Config()
    c.online.enabled = True
    c.pipeline.background = False
    path = tmp_path / "live.toml"
    path.write_text(live_config.render(c))
    text = path.read_text()
    assert "[online]" in text and "background = false" in text
    back = live_config.load(path)
    assert back.online.enabled is True and back.pipeline.background is False


@pytest.mark.parametrize(
    "toml",
    [
        # Nothing reads the review settings with the background off: no error.
        "background = false\ntemporal = false\nanchored = false\n",
        "background = false\ntemporal = false\nanchored_spec = 'generic-release.v2.json'\n",
        "background = false\nanchored_spec = 'generic-final.v1.json'\nanchored_min_valid = 3\n",
    ],
)
def test_background_off_takes_any_review_settings(tmp_path, toml):
    path = tmp_path / "live.toml"
    path.write_text("[pipeline]\n" + toml)
    assert live_config.load(path).pipeline.background is False


def test_background_on_still_checks_the_review_settings(tmp_path):
    path = tmp_path / "live.toml"
    path.write_text(
        "[pipeline]\nbackground = true\ntemporal = false\nanchored = false\n"
    )
    with pytest.raises(ValueError, match="needs pipeline.anchored = true"):
        live_config.load(path)


# --- the endpoint -------------------------------------------------------------------------


@pytest.fixture
def endpoint(tmp_path):
    c = cfg(tmp_path)
    gpu, ask = Gpu(), Ask()
    point = online.Endpoint(c, gpu, ask=ask)
    assert point.start()
    point.gpu, point.ask = gpu, ask
    yield point
    point.stop()


def call(point, method, path, body=None, headers=None, raw=None):
    connection = http.client.HTTPConnection(
        point.config.online.host, point.config.online.port, timeout=scaled(20)
    )
    data = raw if raw is not None else (None if body is None else json.dumps(body))
    sent = {"Content-Type": "application/json"} if data is not None else {}
    sent.update(headers or {})
    connection.request(method, path, body=data, headers=sent)
    response = connection.getresponse()
    value = json.loads(response.read() or b"null")
    connection.close()
    return response.status, value


def test_the_endpoint_listens_on_the_loopback_address_only(endpoint):
    assert endpoint.server.server_address[0] == "127.0.0.1"
    assert endpoint.listening and endpoint.url.startswith("http://127.0.0.1:")
    bad = cfg(endpoint.config.workspace.parent / "other")
    bad.online.host = "0.0.0.0"  # (validation refuses it; the endpoint too)
    with pytest.raises(ValueError, match="loopback"):
        online.Endpoint(bad, Gpu(), ask=Ask()).start()


def test_the_spec_route_says_which_frames_to_send_from_the_spec_file(endpoint):
    code, body = call(endpoint, "GET", "/v1/judge/spec")
    raw = json.loads(generic.text(SPEC))
    assert code == 200
    assert body == {
        "schema": "levi.online.judge.spec.v1",
        "spec_id": raw["id"],
        "spec_version": raw["version"],
        "views": [
            {"role": v["role"], "offsets_seconds": v["offsets_seconds"]}
            for v in raw["views"]
        ],
        "image": {"format": "jpeg", "max_side": online.MAX_SIDE},
        "timeout_s": 15.0,
    }


def test_a_judgement_over_http_answers_the_result_contract(endpoint):
    code, body = call(endpoint, "POST", "/v1/judge", request())
    assert code == 200
    assert set(body) == {
        "schema",
        "status",
        "reason",
        "outcome",
        "undecided",
        "reading",
        "answer",
        "checks",
        "spec",
        "model",
        "tokens",
        "prompt_tokens",
        "elapsed_s",
        "request_id",
    }
    assert body["schema"] == "levi.online.judge.result.v1"
    assert body["status"] == "ok" and body["reason"] is None
    assert body["outcome"] == "success" and body["undecided"] is False
    assert body["reading"] == "supported"
    assert body["answer"] == {"object_state": "resting_at_destination", "stable": "yes"}
    assert body["spec"] == {"id": "generic-final", "version": 1}
    assert body["model"] == "qwen3.8-27b"
    assert (body["tokens"], body["prompt_tokens"]) == (512, 480)
    assert body["request_id"] and body["elapsed_s"] >= 0
    assert endpoint.gpu.admitted == endpoint.gpu.done == 1


@pytest.mark.parametrize(
    "headers,code",
    [
        ({"Origin": "http://evil.example"}, 403),
        ({"Host": "evil.example:80"}, 403),
        ({"Content-Type": "text/plain"}, 415),
    ],
)
def test_a_request_from_a_web_page_or_not_json_is_refused(endpoint, headers, code):
    found, body = call(endpoint, "POST", "/v1/judge", request(), headers=headers)
    assert found == code and body["status"] == "error"
    assert endpoint.ask.calls == []


def test_a_body_over_the_limit_is_refused_with_413_before_it_is_read(tmp_path):
    c = cfg(tmp_path, max_body_mb=0.5)
    ask = Ask()
    point = online.Endpoint(c, Gpu(), ask=ask)
    assert point.start()
    try:
        # Only the headers are sent: a body over the limit is never read.
        with socket.create_connection(("127.0.0.1", c.online.port), timeout=10) as s:
            s.sendall(
                b"POST /v1/judge HTTP/1.1\r\n"
                + f"Host: 127.0.0.1:{c.online.port}\r\n".encode()
                + b"Content-Type: application/json\r\n"
                + b"Content-Length: 900000\r\n\r\n"
            )
            reply = b""
            while chunk := s.recv(65536):
                reply += chunk
        head, _, payload = reply.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 413")
        body = json.loads(payload)
        assert body["status"] == "error" and "max_body_mb" in body["reason"]
        assert ask.calls == []
        (line,) = online.read_log(c.live_dir)
        assert line["http"] == 413 and line["status"] == "error"
        # Without a length the body is not read either.
        code, body = call(point, "POST", "/v1/judge", raw="", headers={})
        assert code in (400, 411)
    finally:
        point.stop()


# --- the strict request ---------------------------------------------------------------------


def _with(**over):
    return request(**over)


def _image_with(**keys):
    body = request()
    body["images"][0].update(keys)
    return body


@pytest.mark.parametrize(
    "body,words",
    [
        (_with(operator_outcome="success"), "operator or evaluation data"),
        (_with(outcome="success"), "operator or evaluation data"),
        (_with(prompt="say success"), "does not define: prompt"),
        (
            request(episode={"demo": "demo_0001", "operator_label": "failure"}),
            "episode has keys",
        ),
        (request(episode={"eval": {"outcome": "failure"}}), "operator or evaluation"),
        (_image_with(label="success"), "images[0] has keys"),
        (_with(schema="levi.online.judge.request.v0"), "schema must be"),
        (_with(task=""), "task must be"),
        (_with(task=["a"]), "task must be"),
        (
            request(images=images(skip={("side", -3.0)})),
            "images missing for side at -3.0 s",
        ),
        (
            request(images=images(extra=[images()[0]])),
            "a second image for side at -3.0 s",
        ),
        (
            request(
                images=images(
                    extra=[{"role": "top", "offset_s": 0.0, "jpeg_b64": "AA=="}]
                )
            ),
            "role 'top' is not one of the spec's views",
        ),
        (_image_with(offset_s=-2.5), "not at -2.5 s"),
        (_image_with(jpeg_b64=base64.b64encode(b"\x89PNG....").decode()), "not a JPEG"),
        (_image_with(jpeg_b64="not base64!"), "not valid base64"),
        (_image_with(step="7"), "step must be an integer"),
        (request(images=[]), "images must be a list"),
    ],
)
def test_a_request_outside_the_contract_is_refused_with_422(tmp_path, body, words):
    ask = Ask()
    judge = judge_with(cfg(tmp_path), ask=ask)
    code, answer = post(judge, body)
    assert code == 422, answer
    assert answer["status"] == "error" and words in answer["reason"], answer["reason"]
    assert answer["outcome"] is None
    assert ask.calls == []  # the model was never asked


def test_a_key_given_twice_or_a_body_that_is_not_json_is_refused(tmp_path):
    judge = judge_with(cfg(tmp_path))
    code, body = judge.handle(b'{"schema": "a", "schema": "b"}')
    assert code == 422 and "appears twice" in body["reason"]
    code, body = judge.handle(b"not json")
    assert code == 400
    code, body = judge.handle(json.dumps(request()).replace("120", "NaN", 1).encode())
    assert code == 422


def test_the_images_reach_the_model_in_the_spec_s_order_whatever_the_request_s(
    tmp_path,
):
    ask = Ask()
    judge = judge_with(cfg(tmp_path), ask=ask)
    shuffled = request()
    order = list(reversed(shuffled["images"]))
    for n, item in enumerate(order):
        item["jpeg_b64"] = base64.b64encode(JPEG + bytes([n])).decode()
    shuffled["images"] = order
    code, body = post(judge, shuffled)
    assert code == 200 and body["status"] == "ok"
    (asked,) = ask.calls
    wanted = [(role, x) for role, offsets in views() for x in offsets]
    by_slot = {
        (i["role"], i["offset_s"]): base64.b64decode(i["jpeg_b64"]) for i in order
    }
    assert asked["files"] == [by_slot[w] for w in wanted]
    assert [n.split("-", 1)[1] for n in asked["names"]] == [
        f"{role}.jpg" for role, _ in wanted
    ]
    # The spec's question with the instruction quoted as the background
    # review quotes it.
    assert asked["question"] == generic.anchored_spec(TASK, SPEC)["question"]
    # The images were deleted once asked.
    assert not list((judge.config.live_dir / online.TMP_DIR).glob("*/*.jpg"))


# --- the same rule as the background review ---------------------------------------------------


@pytest.mark.parametrize(
    "answer,outcome,undecided,reading",
    [
        (
            {"object_state": "resting_at_destination", "stable": "yes"},
            "success",
            False,
            "supported",
        ),
        (
            {"object_state": "elsewhere", "stable": "yes"},
            "failure",
            False,
            "contradicted",
        ),
        (
            {"object_state": "elsewhere", "stable": "unclear"},
            "failure",
            False,
            "contradicted",
        ),
        (
            {"object_state": "held_over_destination", "stable": "yes"},
            "failure",
            False,
            "contradicted",
        ),
        (
            {"object_state": "resting_at_destination", "stable": "no"},
            "failure",
            False,
            "contradicted",
        ),
        (
            {"object_state": "resting_at_destination", "stable": "unclear"},
            "failure",
            True,
            "unknown",
        ),
        ({"object_state": "unclear", "stable": "yes"}, "failure", True, "unknown"),
    ],
)
def test_the_outcome_is_the_final_state_rule_of_the_background_review(
    tmp_path, answer, outcome, undecided, reading
):
    judge = judge_with(cfg(tmp_path), ask=Ask(answer))
    code, body = post(judge, request())
    assert code == 200 and body["status"] == "ok"
    # What the background review's own functions say for the same answer.
    spec = anchored.AnchoredSpec.model_validate(generic.anchored_spec(TASK, SPEC))
    checks, verdict = anchored.judge(spec, answer)
    expected, basis = anchored.outcome(
        spec, [{"answer": answer, "verdict": verdict, "valid": verdict == "supported"}]
    )
    assert (body["outcome"], body["undecided"], body["reading"]) == (
        expected,
        anchored.undecided(expected, basis),
        verdict,
    )
    assert (body["outcome"], body["undecided"], body["reading"]) == (
        outcome,
        undecided,
        reading,
    )
    assert body["checks"] == checks and body["answer"] == answer


def test_an_answer_outside_the_spec_is_an_error_not_a_verdict(tmp_path):
    judge = judge_with(
        cfg(tmp_path), ask=Ask({"object_state": "on_the_moon", "stable": "yes"})
    )
    code, body = post(judge, request())
    assert code == 200 and body["status"] == "error"
    assert body["reason"].startswith("invalid_answer") and body["outcome"] is None


# --- unavailable fast, errors, one at a time -------------------------------------------------


@pytest.mark.parametrize(
    "admit",
    [
        (False, "cold_start_needed", "vLLM is not running"),
        (False, "gate_closed", "policy_inferring: the policy is inferring"),
        (False, "no_room", "vLLM is asleep and cannot wake"),
    ],
)
def test_refused_admission_is_unavailable_at_once(tmp_path, admit):
    gpu, ask = Gpu(), Ask()
    gpu.admit = admit
    judge = judge_with(cfg(tmp_path), gpu, ask)
    began = time.monotonic()
    code, body = post(judge, request())
    assert time.monotonic() - began < 1.0
    assert code == 200 and body["status"] == "unavailable"
    assert body["reason"].startswith(admit[1]) and body["outcome"] is None
    assert ask.calls == [] and gpu.done == 0


def test_one_judgement_at_a_time_the_second_is_unavailable_at_once(tmp_path):
    ask = Ask(hold=scaled(10))
    gpu = Gpu()
    judge = judge_with(cfg(tmp_path), gpu, ask)
    first = {}
    thread = threading.Thread(target=lambda: first.update(r=post(judge, request())))
    thread.start()
    deadline = time.time() + scaled(5)
    while not ask.calls and time.time() < deadline:
        time.sleep(0.02)
    began = time.monotonic()
    code, body = post(judge, request())
    assert time.monotonic() - began < 1.0
    assert code == 200
    assert body["status"] == "unavailable" and body["reason"].startswith("busy")
    ask.release.set()
    thread.join(scaled(10))
    assert first["r"][1]["status"] == "ok" and gpu.done == 1


def test_a_model_that_does_not_answer_in_time_is_an_error_timeout(tmp_path):
    ask = Ask(hold=scaled(10))
    gpu = Gpu()
    judge = judge_with(cfg(tmp_path, timeout_s=1.0), gpu, ask)
    began = time.monotonic()
    code, body = post(judge, request())
    took = time.monotonic() - began
    ask.release.set()
    assert code == 200 and body["status"] == "error"
    assert body["reason"].startswith("timeout") and body["outcome"] is None
    assert 0.9 <= took < 3.0
    assert gpu.done == 1  # the supervisor's hold was let go


def test_a_gate_that_closes_while_the_model_works_cuts_the_request(tmp_path):
    ask = Ask(hold=scaled(10))
    gpu = Gpu()
    judge = judge_with(cfg(tmp_path), gpu, ask)
    gpu.check = (False, "gate_closed", "policy_inferring: g/t is running")
    began = time.monotonic()
    code, body = post(judge, request())
    ask.release.set()
    assert code == 200
    assert time.monotonic() - began < 1.5
    assert body["status"] == "unavailable" and body["reason"].startswith("gate_closed")
    assert gpu.done == 1


def test_the_supervisor_can_cut_an_answer_in_progress(tmp_path):
    ask = Ask(hold=scaled(10))
    judge = judge_with(cfg(tmp_path), Gpu(), ask)
    found = {}
    thread = threading.Thread(target=lambda: found.update(r=post(judge, request())))
    thread.start()
    deadline = time.time() + scaled(5)
    while not ask.calls and time.time() < deadline:
        time.sleep(0.02)
    assert judge.abort("vllm_sleeping: free VRAM fell to 300 MiB")
    thread.join(scaled(5))
    ask.release.set()
    assert found["r"][1]["status"] == "unavailable"
    assert found["r"][1]["reason"].startswith("vllm_sleeping")
    assert not judge.abort("nothing in progress")


# --- the model request: images and the question, nothing else ----------------------------------


def test_the_model_request_holds_the_images_and_the_question_and_no_metadata(tmp_path):
    server, fake, port = fakevlm.serve(
        0, fakevlm.Fake(answers={"object_state": "elsewhere", "stable": "yes"})
    )
    try:
        c = cfg(tmp_path)
        judge = online.Judge(c, Gpu(port))  # the real model path
        code, body = post(judge, request())
        assert code == 200 and body["status"] == "ok", body
        assert body["outcome"] == "failure" and body["reading"] == "contradicted"
        assert body["tokens"] and body["prompt_tokens"] == 400
        (payload,) = [p for p in fake.calls]
    finally:
        server.shutdown()
    dumped = json.dumps(payload)
    for marker in ("RUN-MARKER-7f3a", "demo_0042", "pi05_group_marker", "cups"):
        assert marker not in dumped
    assert "operator" not in dumped.lower()
    (message,) = payload["messages"]
    parts = message["content"]
    pictures = [p for p in parts if p["type"] == "image_url"]
    texts = [p["text"] for p in parts if p["type"] == "text"]
    assert len(pictures) == 10
    assert all(
        p["image_url"]["url"].startswith("data:image/jpeg;base64,") for p in pictures
    )
    assert texts == [generic.anchored_spec(TASK, SPEC)["question"]]
    assert set(payload) <= {
        "model",
        "messages",
        "response_format",
        "max_completion_tokens",
        "stream",
        "chat_template_kwargs",
    }
    spec = json.loads(generic.text(SPEC))
    assert payload["max_completion_tokens"] == spec["max_output_tokens"]
    schema = payload["response_format"]["json_schema"]["schema"]
    assert schema["required"] == ["object_state", "stable"]


# --- the log ---------------------------------------------------------------------------------


def test_every_judgement_adds_a_line_to_online_jsonl_without_images(tmp_path):
    c = cfg(tmp_path)
    gpu = Gpu()
    judge = judge_with(c, gpu)
    post(judge, request())
    gpu.admit = (False, "gate_closed", "policy_inferring")
    post(judge, request())
    post(judge, request(outcome="success"))
    rows = online.read_log(c.live_dir)
    assert [(r["status"], r["http"]) for r in rows] == [
        ("ok", 200),
        ("unavailable", 200),
        ("error", 422),
    ]
    first = rows[0]
    assert first["schema"] == "levi.live.online.v1"
    assert first["episode"]["demo"] == "demo_0042" and first["images"] == 10
    assert first["outcome"] == "success" and first["undecided"] is False
    assert first["tokens"] == 512 and first["spec"] == {
        "id": "generic-final",
        "version": 1,
    }
    text = (c.live_dir / online.LOG_FILE).read_text()
    assert base64.b64encode(JPEG).decode() not in text and TASK not in text


def test_the_log_rotates_like_the_other_logs(tmp_path):
    c = cfg(tmp_path)
    c.resources.log_max_mb = 1
    log = c.live_dir / online.LOG_FILE
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("x" * (1024 * 1024 + 1) + "\n")
    post(judge_with(c), request())
    assert log.with_name(online.LOG_FILE + ".1").is_file()
    (line,) = online.read_log(c.live_dir)
    assert line["status"] == "ok"


# --- the supervisor: admission, status file --------------------------------------------------


@pytest.fixture
def rollouts(tmp_path, demo_template):
    return Rollouts(tmp_path / "rollouts", demo_template)


def client_usable(status, root, now=None):
    """The evaluation client's check of the status file (interface C4)."""
    import os

    now = time.time() if now is None else now
    roots = [Path(r) for r in status.get("watch_roots") or []]
    try:
        os.kill(int(status["pid"]), 0)
        alive = True
    except (OSError, KeyError, ValueError):
        alive = False
    return (
        str(status.get("schema", "")).startswith("levi.live.status.")
        and now - float(status.get("updated_at") or 0) < 15
        and alive
        and status.get("accepts_sessions") is True
        and status.get("state") in ("idle", "active", "annotating", "gpu_wait")
        and any(Path(root) == r or r in Path(root).parents for r in roots)
    )


def test_the_status_file_names_the_endpoint_and_the_client_check_still_holds(
    tmp_path, rollouts
):
    c = cfg(tmp_path)
    (tmp_path / "rollouts").mkdir(exist_ok=True)
    ctl = controller.Controller(c, log=lambda *a: None)
    try:
        assert ctl.start_online()
        ctl.tick()
        ctl.write_status()
        status = json.loads(c.status_file.read_text())
        assert status["online_judge"] == {
            "url": f"http://127.0.0.1:{c.online.port}",
            "spec": "generic-final",
            "spec_version": 1,
            "ready": False,  # manual mode, no model server seen
        }
        assert client_usable(status, tmp_path / "rollouts")
    finally:
        ctl.shutdown()
    off = cfg(tmp_path / "off")
    off.online.enabled = False
    quiet = controller.Controller(off, log=lambda *a: None)
    try:
        assert not quiet.start_online() and quiet.online is None
        status = quiet.status()
        assert status["online_judge"] is None
        assert "online_judge" in status
        assert not (off.live_dir / online.LOG_FILE).exists()
    finally:
        quiet.shutdown()


def test_the_supervisor_admits_only_with_an_open_gate_and_a_model_it_may_use(
    tmp_path, rollouts
):
    c = cfg(tmp_path)
    c.gpu.mode = "timeshare"
    ctl = controller.Controller(c, log=lambda *a: None)
    try:
        now = time.time()
        # No vLLM: never a cold start for an online judgement.
        began = time.monotonic()
        ok, code, _ = ctl.online_admit(now)
        assert (ok, code) == (False, "cold_start_needed")
        assert time.monotonic() - began < 1.0
        # The policy infers: the gate is closed, read from the session file now.
        rollouts.session("running")
        ok, code, why = ctl.online_admit(now)
        assert (ok, code) == (False, "gate_closed") and "policy_inferring" in why
        # Starting: not yet.
        rollouts.session("homing")
        ctl.vllm.state = "starting"
        ctl.vllm.mine = lambda: True
        assert ctl.online_admit(now)[1] == "vllm_starting"
        # Ready and the gate open: admitted, and vLLM is kept awake meanwhile.
        ctl.vllm.state = "ready"
        ctl.idle_since = now - 1000
        assert ctl.online_admit(now) == (True, None, None)
        assert ctl._online_busy and ctl.idle_since is None
        ok, _, _ = ctl.online_check(now)
        assert ok
        rollouts.session("running")
        ok, code, _ = ctl.online_check(now)
        assert (ok, code) == (False, "gate_closed")
        ctl.online_done()
        assert not ctl._online_busy
        # The tick's own GPU work holds the mutex: admission does not wait.
        ctl._gpu_mutex.acquire()
        try:
            result = {}
            thread = threading.Thread(
                target=lambda: result.update(r=ctl.online_admit(now))
            )
            began = time.monotonic()
            thread.start()
            thread.join(scaled(5))
            assert result["r"][1] == "service_busy"
            assert time.monotonic() - began < 1.5
        finally:
            ctl._gpu_mutex.release()
    finally:
        ctl.vllm.mine = lambda: False
        ctl.shutdown()


def test_a_sleeping_vllm_is_woken_only_when_the_gpu_rules_allow(tmp_path, rollouts):
    c = cfg(tmp_path)
    c.gpu.mode = "timeshare"
    free = {"mib": 2000}
    probes = controller.Probes(
        vram=lambda: {"total_mib": 32607, "used_mib": 0, "free_mib": free["mib"]},
        ports=set,
        holders=list,
        policy_vram=lambda ports: None,
    )
    ctl = controller.Controller(c, probes=probes, log=lambda *a: None)
    woke = []
    try:
        ctl.vllm.mine = lambda: True
        ctl.vllm.state = "asleep"
        ctl.vllm.profile = c.vllm_profile()

        def wake(timeout=60.0):
            woke.append(timeout)
            ctl.vllm.state = "ready"
            return True

        ctl.vllm.wake = wake
        now = time.time()
        ok, code, why = ctl.online_admit(now)
        assert (ok, code) == (False, "no_room") and woke == []
        assert "cannot wake" in why
        free["mib"] = 30000
        ctl._vram = (0.0, None)
        assert ctl.online_admit(now) == (True, None, None)
        assert woke == [5.0] and ctl.vllm.state == "ready"
        ctl.online_done()
    finally:
        ctl.vllm.mine = lambda: False
        ctl.shutdown()


def test_putting_vllm_to_sleep_cuts_an_online_judgement(tmp_path):
    c = cfg(tmp_path)
    ctl = controller.Controller(c, log=lambda *a: None)
    cut = []

    class Point:
        class judge:
            @staticmethod
            def abort(reason):
                cut.append(reason)
                return True

        def stop(self):
            pass

    ctl.online = Point()
    try:
        ctl.vllm.sleep = lambda: True
        ctl.sleep_vllm("vram", "free VRAM fell to 300 MiB")
        assert cut and cut[0].startswith("vllm_sleeping")
    finally:
        ctl.shutdown()


# --- the relayed result back in the dataset state ------------------------------------------


def label(rollouts, n, **eval_fields):
    path = rollouts.demo(n) / "metadata.json"
    meta = json.loads(path.read_text())
    meta["eval"].update(eval_fields)
    path.write_text(json.dumps(meta))


def relayed(outcome="success", undecided=False, status="ok", **more):
    if status != "ok":
        return {
            "source": "online",
            "status": status,
            "reason": "gate_closed: x",
            **more,
        }
    body = online.result(
        "ok",
        request_id="abc123",
        spec={"id": "generic-final", "version": 1},
        model="qwen3.8-27b",
        elapsed=2.5,
        answer={"object_state": "elsewhere", "stable": "yes"},
        reading="supported" if outcome == "success" else "contradicted",
        outcome=outcome,
        undecided=undecided,
        tokens=900,
        prompt_tokens=850,
    )
    return {
        **body,
        "source": "online",
        "requested_at": 1790000000.0,
        "received_at": 1790000002.5,
        "frames": [{"role": "side", "offset_s": 0.0, "step": 120}],
        "timing": "during_run",
        **more,
    }


def test_the_agent_label_reader_gives_the_worker_s_verdict_shape():
    meta = {"eval": {"agent_label": relayed("failure", undecided=True)}}
    found = criteria.agent_label(meta)
    verdict = found["verdict"]
    assert found["status"] == "ok" and found["timing"] == "during_run"
    assert verdict["outcome"] == "failure" and verdict["undecided"] is True
    assert verdict["source"] == "online" and verdict["rule"] == "final_state"
    assert (verdict["events"], verdict["valid_events"]) == (1, 0)
    assert (verdict["review"], verdict["evaluated"]) == ("auto", False)
    assert (verdict["spec"], verdict["spec_version"]) == ("generic-final", 1)
    assert verdict["at"] == 1790000002.5
    assert stats.agent_of(verdict) == "undecided"
    # Not ok: no verdict (no_agent), the reason kept.
    for status in ("unavailable", "error", "timeout", "skipped"):
        found = criteria.agent_label({"eval": {"agent_label": relayed(status=status)}})
        assert found["status"] == status and found["verdict"] is None
    # A malformed ok is no verdict either; a label of another source is none.
    bad = relayed()
    bad["outcome"] = "maybe"
    assert criteria.agent_label({"eval": {"agent_label": bad}})["verdict"] is None
    assert criteria.agent_label({"eval": {"agent_label": {"status": "ok"}}}) is None
    assert criteria.agent_label({"eval": {}}) is None


def background_off(tmp_path):
    c = cfg(tmp_path)
    c.online.enabled = False
    c.pipeline.background = False
    return c


def test_background_off_mirrors_takes_in_and_records_without_a_worker(
    tmp_path, rollouts
):
    c = background_off(tmp_path)
    rollouts.write(0)
    label(
        rollouts,
        0,
        outcome="failure",
        verdict_by="operator",
        operator_outcome="failure",
        ended_by="operator_key",
        agent_label=relayed("success"),
    )
    rollouts.write(1)
    label(
        rollouts,
        1,
        outcome="success",
        verdict_by="operator",
        operator_outcome="success",
        agent_label=relayed(status="unavailable"),
    )
    rollouts.write(2)
    label(
        rollouts,
        2,
        outcome="success",
        verdict_by="operator",
        operator_outcome="success",
    )
    ctl = controller.Controller(c, log=lambda *a: None)
    spawned = []
    ctl._spawn = lambda name, now=None: spawned.append(name)
    try:
        for _ in range(3):
            ctl.tick()
        assert spawned == [] and ctl.worker is None
        assert not ctl.vllm.mine() and ctl.vllm.state == "stopped"
        name = "pi05_fake__stack_the_plates"
        state = mirror.load_state(c, name)
        rows = state["demos"]
        assert {d: r["state"] for d, r in rows.items()} == {
            "demo_0000": "done",
            "demo_0001": "done",
            "demo_0002": "done",
        }
        assert (c.captures_dir / name / "demo_0000" / "metadata.json").is_file()
        assert rows["demo_0000"]["verdict"]["source"] == "online"
        assert rows["demo_0000"]["verdict"]["outcome"] == "success"
        assert rows["demo_0000"]["operator_label"]["outcome"] == "failure"
        assert "verdict" not in rows["demo_0001"]
        assert "unavailable" in rows["demo_0001"]["reason"]
        assert "no online judgement" in rows["demo_0002"]["reason"]
        # The status: nothing pending, sessions still welcome.
        status = ctl.status()
        row = status["datasets"][name]
        assert (row["pending"], row["done"]) == (0, 3)
        assert status["accepts_sessions"] is True and status["queue_depth"] == 0
        # The statistics and their agreement, as the page and the report read them.
        records = stats.read(c.live_dir)
        assert sorted(r["demo"] for r in records) == [
            "demo_0000",
            "demo_0001",
            "demo_0002",
        ]
        first = next(r for r in records if r["demo"] == "demo_0000")
        assert first["result"]["verdict"]["outcome"] == "success"
        assert first["result"]["verdict"]["source"] == "online"
        assert first["model"]["requests"]["review"] == 1
        assert first["model"]["total_tokens"] == 900
        assert first["operator_label"]["ended_by"] == "operator_key"
        found = stats.summarize(records)["agreement"]
        assert found["pairs"] == 3 and found["no_agent"] == 2
        assert found["false_success"]["n"] == 1
        page = api._demo_row("demo_0000", rows["demo_0000"])
        assert page["verdict"]["source"] == "online" and page["agreement"] == "no"
        assert api._demo_row("demo_0001", rows["demo_0001"])["agreement"] == "no_agent"
        # Taken in once: another tick adds no record.
        ctl.tick()
        assert len(stats.read(c.live_dir)) == 3
    finally:
        ctl.shutdown()


def test_background_on_keeps_the_online_verdict_until_a_review_replaces_it(
    tmp_path, rollouts
):
    c = cfg(tmp_path)
    c.online.enabled = False
    rollouts.write(0)
    label(rollouts, 0, agent_label=relayed("success"))
    scanner = mirror.Scanner(c)
    (task,) = scanner.scan()
    mirror.mirror_dataset(c, mirror.load_state(c, task.name), task.ready)
    row = mirror.load_state(c, task.name)["demos"]["demo_0000"]
    # Still waiting for the background labelling, with the online verdict.
    assert row["state"] == "mirrored"
    assert row["verdict"]["source"] == "online"
    assert row["online"]["verdict"] == row["verdict"]


# --- end to end: the supervisor, a stand-in vLLM it started, the endpoint ---------------------


def test_the_service_wakes_its_vllm_for_a_judgement_and_never_meets_the_policy(
    live,  # noqa: F811 - the fixture imported above
):
    c, rollouts = live
    c.online.enabled = True
    c.online.port = free_port()
    c.validate()
    machine = Machine()
    machine.ports, machine.policy_mib = {8000}, 7685
    ctl = controller.Controller(c, probes=machine.probes(), log=lambda *a: None)
    ctl._spawn = lambda name, now=None: None
    try:
        rollouts.write(0)  # work, so that vLLM starts (as a batch would start it)
        rollouts.session("standby")
        t = time.time()
        assert ctl.tick(t) is not None and ctl.decision.code == "ok"
        assert wait_for(lambda: ctl.vllm.poll() == "ready")
        ctl.tick(t + 0.5)
        ctl.sleep_vllm("test", "for the test")
        assert ctl.vllm.state == "asleep"
        assert ctl.start_online()
        point = ctl.online
        assert ctl.status()["online_judge"]["ready"] is True
        # Between episodes: the gate is open, vLLM is woken and answers.
        rollouts.session("homing")
        code, body = call(point, "POST", "/v1/judge", request())
        assert code == 200 and body["status"] == "ok", body
        assert body["outcome"] == "success" and body["model"] == c.vllm.served_model
        assert ctl.vllm.state == "ready" and not ctl._online_busy
        # The policy infers: unavailable at once, nothing sent to the model.
        rollouts.session("running")
        began = time.monotonic()
        code, body = call(point, "POST", "/v1/judge", request())
        assert time.monotonic() - began < 1.0
        assert body["status"] == "unavailable" and body["reason"].startswith(
            "gate_closed: policy_inferring"
        )
        # The gate file says the same at once (the worker and the core read it).
        gate = json.loads((c.live_dir / "gate.json").read_text())
        assert gate["open"] is False and gate["code"] == "policy_inferring"
        rows = online.read_log(c.live_dir)
        assert [r["status"] for r in rows] == ["ok", "unavailable"]
    finally:
        ctl.shutdown()
    assert ctl.online is None


def test_a_disabled_endpoint_s_port_clashes_with_nothing(tmp_path):
    path = tmp_path / "live.toml"
    path.write_text("[service]\nui_port = 7882\n")
    assert live_config.load(path).service.ui_port == 7882


def test_a_report_states_background_off_and_the_online_judgement_only_when_set():
    from levi.live import report

    c = live_config.Config()
    plain = report.settings(c)
    assert "background" not in plain["pipeline"] and "online" not in plain
    c.pipeline.background = False
    c.online.enabled = True
    found = report.settings(c)
    assert found["pipeline"]["background"] is False
    assert found["online"] == {
        "enabled": True,
        "spec": "generic-final.v1.json",
        "timeout_s": 15.0,
    }
