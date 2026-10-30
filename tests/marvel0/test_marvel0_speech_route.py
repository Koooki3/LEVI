"""``levi/marvel0/speech_api.py``: the four speech routes on a router of their
own, with a fake policy status, a fake worker manager and a stub matcher (the
real manager is in ``test_marvel0_speech_route_real.py``). What the router
adds: the person-only rule, the site file first, the checks on the request
before the audio is read, the byte cap, the WAV rules, the guards, the status
of every refusal, the rate limit, and that no answer names a path."""

import asyncio
import copy
import itertools
import re
import struct
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from site_sample import make_checkpoint, make_checkpoints, write_site

from levi.marvel0 import deploy_api, models, speech_api
from levi.marvel0 import site as marvel0_site
from levi.stt import settings
from levi.stt.errors import SttError
from levi.stt.manager import Transcript

BASE = "/api/levi/marvel0/speech"
REQ = "speech-req-0001"
TRANSCRIBE = f"{BASE}/transcribe?request_id={REQ}"
HEARD = "pick the eggplant in the blue plate"
PATH_IN_TEXT = re.compile(r"(?<![\w.:-])/(?:[A-Za-z0-9_.~+@%=,-]+/)+")

# The status of every refusal of this module, written out from the contract
# (docs/SPEECH.md), not read back from SPEECH_REFUSALS.
CONTRACT = {
    "request_id_invalid": 400,
    "speech_body_invalid": 400,
    "audio_too_short": 400,
    "audio_too_large": 413,
    "audio_format": 415,
    "speech_not_installed": 503,
    "speech_model_missing": 503,
    "speech_manifest_mismatch": 503,
    "speech_gpu_unreadable": 503,
    "speech_policy_starting": 409,
    "speech_client_connected": 409,
    "speech_arm_in_use": 409,
    "speech_gpu_insufficient": 409,
    "speech_allowed_set_invalid": 412,
    "speech_busy": 429,
    "speech_failed": 502,
    "speech_timeout": 504,
}


def wav_bytes(seconds=1.0, *, samples=None, **fields):
    """A 16 kHz mono 16-bit WAV of ``seconds`` (``fields`` break the header)."""
    count = samples if samples is not None else int(seconds * 16_000)
    pcm = b"\x01\x02" * count
    values = {
        "riff_size": 36 + len(pcm),
        "fmt_size": 16,
        "audio_format": 1,
        "channels": 1,
        "rate": 16_000,
        "byte_rate": 32_000,
        "block_align": 2,
        "bits": 16,
        "data_size": len(pcm),
        "tail": b"",
        **fields,
    }
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        values["riff_size"],
        b"WAVE",
        b"fmt ",
        values["fmt_size"],
        values["audio_format"],
        values["channels"],
        values["rate"],
        values["byte_rate"],
        values["block_align"],
        values["bits"],
        b"data",
        values["data_size"],
    )
    return header + pcm + values["tail"]


def policy_status(
    *, state="ready", established=0, arm="ok", gpu="ok", free_mib=20_000, drop=()
):
    """The parts of the deploy status the guards read."""
    doc = {
        "state": state,
        "listener": {
            "port": 8000,
            "present": True,
            "ours": True,
            "established": established,
        },
        "checks": [
            {"code": "arm_lock", "level": arm, "text": "x", "params": {}},
            {"code": "vram", "level": "ok", "text": "x", "params": {}},
        ],
        "gpu": {
            "state": gpu,
            "total_mib": 32_607,
            "used_mib": None if gpu != "ok" else 32_607 - free_mib,
            "free_mib": free_mib if gpu == "ok" else None,
        },
    }
    for key in drop:
        doc.pop(key)
    return doc


class Policy:
    """Stands for ``deploy.Supervisor``: only ``status()``."""

    def __init__(self):
        self.doc = policy_status()
        self.error = None
        self.reads = 0

    def status(self):
        self.reads += 1
        if self.error:
            raise self.error
        return copy.deepcopy(self.doc)


class FakeManager:
    """Stands for ``levi.stt.manager.Manager``."""

    def __init__(self):
        self.worker = settings.WorkerState(True, True)
        self.view = {
            "state": "off",
            "reason": None,
            "model": "qwen3-asr-1.7b-hf",
            "device": "cuda",
            "idle_in_s": None,
        }
        self.answer = transcript(HEARD)
        self.error = None
        self.calls = []
        self.released = 0
        self.cold = False
        self.asked = 0

    def installed(self):
        return self.worker

    def status(self):
        return dict(self.view)

    def transcribe(self, request_id, wav, cpu_affinity="", may_start=None):
        self.calls.append((request_id, wav, cpu_affinity))
        # ``cold``: the worker the status showed has gone meanwhile (idle exit,
        # crash), so the manager must start one and asks ``may_start`` first.
        if may_start is not None and (self.cold or self.view["state"] == "off"):
            self.asked += 1
            may_start()
        if self.error:
            raise self.error
        return self.answer

    def release(self):
        self.released += 1
        return True


def transcript(text, *, heard=True, reason=None, loaded_now=True, load=900, infer=104):
    return Transcript(
        heard=heard,
        reason=reason,
        text=text if heard else "",
        language="English" if heard else None,
        loaded_now=loaded_now,
        load_ms=load,
        infer_ms=infer,
    )


class Matcher:
    """Stands for ``levi.marvel0.speech_match``: records what it was asked."""

    def __init__(self):
        self.checked = []
        self.calls = []
        self.problems = []
        self.error = None

    def check_allowed_set(self, allowed):
        self.checked.append(tuple(allowed))
        return list(self.problems)

    def match_transcript(self, text, allowed, *, family):
        self.calls.append((text, tuple(allowed), family))
        if self.error:
            raise self.error
        return {
            "tier": "A",
            "flag": None,
            "family": family,
            "best": {
                "prompt": allowed[0],
                "score": 100,
                "fixes": [],
                "differences": [],
            },
            "alternatives": [],
            "allowed": list(allowed),
        }


class Machine:
    def __init__(self, client, tmp_path, policy, manager, matcher):
        self.client, self.tmp_path = client, tmp_path
        self.policy, self.manager, self.matcher = policy, manager, matcher

    def post(self, body=None, *, headers=None, query=f"?request_id={REQ}"):
        return self.client.post(
            f"{BASE}/transcribe{query}",
            content=wav_bytes() if body is None else body,
            headers={"content-type": "audio/wav", **(headers or {})},
        )


@pytest.fixture(autouse=True)
def no_token(monkeypatch):
    monkeypatch.delenv("LEVI_UI_TOKEN", raising=False)


def build_machine(tmp_path, monkeypatch, manager, matcher=None):
    """A router with the fake machine around it; ``matcher=None`` leaves the
    real matcher in place. Returns the ``Machine`` (the caller clears the
    site and model caches afterwards, see ``machine``)."""
    root = make_checkpoints(tmp_path, names=("pi05_fr3_all_step49999",))
    make_checkpoint(root, "pi05_fr3_sp_v1", config="pi05_fr3_all_state")
    monkeypatch.setenv("LEVI_MARVEL0_SITE", str(write_site(tmp_path)))
    marvel0_site.clear_cache()
    models.cache_clear()
    policy = Policy()
    monkeypatch.setattr(deploy_api, "supervisor", lambda: policy)
    monkeypatch.setattr(speech_api, "manager", lambda: manager)
    if matcher is not None:
        monkeypatch.setattr(speech_api, "_matcher", lambda: matcher)
    monkeypatch.setattr(speech_api, "RATE", speech_api.RateLimit(10, 60.0))
    app = FastAPI()
    app.include_router(speech_api.router)
    return Machine(TestClient(app), tmp_path, policy, manager, matcher)


@pytest.fixture
def machine(tmp_path, monkeypatch):
    yield build_machine(tmp_path, monkeypatch, FakeManager(), Matcher())
    marvel0_site.clear_cache()
    models.cache_clear()


def code_of(response):
    return response.json()["detail"]["code"]


def assert_refused(machine, response, code):
    assert response.status_code == CONTRACT.get(code, response.status_code), (
        response.text
    )
    assert code_of(response) == code, response.text
    assert machine.manager.calls == [], "the worker must not have been asked"


# --------------------------------------------------------------- the contract


def test_the_status_of_every_refusal_is_the_contracts():
    assert {c: s for c, (s, _) in speech_api.SPEECH_REFUSALS.items()} == CONTRACT


def test_every_code_the_module_answers_with_is_in_its_table():
    source = Path(speech_api.__file__).read_text()
    used = set(re.findall(r'refuse\(\s*"([a-z_]+)"', source))
    used |= set(speech_api.GATE_CODES.values()) | set(speech_api.WAV_CODES.values())
    used |= set(speech_api.STT_CODES.values())
    assert used <= set(speech_api.SPEECH_REFUSALS), used - set(
        speech_api.SPEECH_REFUSALS
    )


def test_the_table_is_laid_out_for_the_page_test_that_reads_it_from_source():
    text = Path(speech_api.__file__).read_text()
    start = text.index("\nSPEECH_REFUSALS = {")
    end = text.index("\n}\n", start)
    keys = re.findall(r'^ {4}"([a-z_]+)": ', text[start:end], re.MULTILINE)
    assert sorted(keys) == sorted(CONTRACT)


def test_the_routes_are_these_four_under_the_marvel0_prefix():
    found = sorted(
        (route.path, sorted(route.methods - {"HEAD", "OPTIONS"}))
        for route in speech_api.router.routes
    )
    assert found == [
        (BASE, ["GET"]),
        (f"{BASE}/match", ["POST"]),
        (f"{BASE}/release", ["POST"]),
        (f"{BASE}/transcribe", ["POST"]),
    ]


def test_a_refusal_carries_only_the_facts_that_belong_to_it(machine):
    plain = machine.post(query="?request_id=bad").json()["detail"]
    assert set(plain) == {"code", "message"}
    large = machine.post(b"x" * (settings.MAX_BYTES + 1)).json()["detail"]
    assert (large["max_seconds"], large["max_bytes"]) == (10, 400000)
    assert "400000" in large["message"] and "10 seconds" in large["message"]


def test_no_refusal_message_names_a_path():
    for status, text in speech_api.SPEECH_REFUSALS.values():
        assert not PATH_IN_TEXT.search(text), text


# --------------------------------------------------------------- who may ask


ROUTES = [
    ("GET", BASE, None),
    ("POST", f"{BASE}/transcribe?request_id={REQ}", wav_bytes()),
    ("POST", f"{BASE}/match", b'{"transcript": "x"}'),
    ("POST", f"{BASE}/release", None),
]


@pytest.mark.parametrize("method, url, body", ROUTES)
def test_a_bearer_credential_is_refused_on_every_route(machine, method, url, body):
    response = machine.client.request(
        method,
        url,
        content=body,
        headers={"authorization": "Bearer not-an-agent-credential"},
    )
    assert response.status_code == 403 and code_of(response) == "person_only"
    assert machine.manager.calls == [] and machine.manager.released == 0
    assert machine.policy.reads == 0


@pytest.mark.parametrize("method, url, body", ROUTES)
def test_the_web_ui_token_is_needed_when_one_is_set(
    machine, monkeypatch, method, url, body
):
    monkeypatch.setenv("LEVI_UI_TOKEN", "the-token")
    refused = machine.client.request(method, url, content=body)
    assert refused.status_code == 401 and code_of(refused) == "ui_token"
    wrong = machine.client.request(
        method, url, content=body, headers={"x-levi-ui-token": "other"}
    )
    assert wrong.status_code == 401
    assert machine.manager.calls == [] and machine.manager.released == 0
    allowed = machine.client.request(
        method,
        url,
        content=body,
        headers={"x-levi-ui-token": "the-token", "content-type": "audio/wav"},
    )
    assert allowed.status_code not in (401, 403), allowed.text


@pytest.mark.parametrize("method, url, body", ROUTES)
@pytest.mark.parametrize("state", ["missing", "invalid"])
def test_without_a_valid_site_file_every_route_answers_412_first(
    machine, monkeypatch, method, url, body, state
):
    target = machine.tmp_path / "gone.toml"
    if state == "invalid":
        target.write_text("this is = not [valid")
        target.chmod(0o644)
    monkeypatch.setenv("LEVI_MARVEL0_SITE", str(target))
    marvel0_site.clear_cache()
    response = machine.client.request(
        method, url, content=body, headers={"content-type": "audio/wav"}
    )
    assert response.status_code == 412
    assert code_of(response) in ("site_missing", "site_invalid")
    assert machine.manager.calls == [] and machine.manager.released == 0
    assert machine.policy.reads == 0


# --------------------------------------------------------------- the request


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?request_id=",
        "?request_id=short",
        f"?request_id={'a' * 81}",
        "?request_id=a b c d e f g",
        "?request_id=a/b/c/d/e/f/g",
        "?id=" + REQ,
    ],
    ids=["none", "empty", "short", "81-chars", "space", "slash", "wrong-name"],
)
def test_a_bad_request_id_is_refused(machine, query):
    assert_refused(machine, machine.post(query=query), "request_id_invalid")


def test_the_request_id_may_be_any_of_the_allowed_characters(machine):
    assert machine.post(query="?request_id=A.b_c:d-12").status_code == 200
    assert machine.post(query=f"?request_id={'a' * 80}").status_code == 200


@pytest.mark.parametrize(
    "content_type",
    [None, "audio/webm", "application/json", "audio/x-wav", "text/plain", "audio/wave"],
)
def test_only_audio_wav_is_accepted_as_a_content_type(machine, content_type):
    headers = {"content-type": content_type} if content_type else {}
    response = machine.client.post(TRANSCRIBE, content=wav_bytes(), headers=headers)
    assert_refused(machine, response, "audio_format")


@pytest.mark.parametrize(
    "content_type", ["audio/wav", "AUDIO/WAV", "audio/wav; rate=16000"]
)
def test_the_media_type_ignores_case_and_parameters(machine, content_type):
    response = machine.post(headers={"content-type": content_type})
    assert response.status_code == 200, response.text


def test_a_refusal_before_the_body_does_not_use_a_slot_of_the_rate_limit(machine):
    for _ in range(30):
        machine.post(query="?request_id=bad")
        machine.client.post(TRANSCRIBE, content=wav_bytes())
    assert machine.post().status_code == 200


def test_an_unknown_model_is_a_422(machine):
    response = machine.post(query=f"?request_id={REQ}&model_id=no-such-model")
    assert response.status_code == 422 and code_of(response) == "model_unknown"
    assert machine.manager.calls == []


# --------------------------------------------------------------- the WAV


GOOD = wav_bytes(1.0)
WAV_REFUSALS = {
    "garbage": (b"x" * 5000, "audio_format"),
    "empty": (b"", "audio_format"),
    "short-header": (GOOD[:43], "audio_format"),
    "wrong-rate": (wav_bytes(rate=48_000, byte_rate=96_000), "audio_format"),
    "stereo": (wav_bytes(channels=2, byte_rate=64_000, block_align=4), "audio_format"),
    "8-bit": (wav_bytes(bits=8, byte_rate=16_000, block_align=1), "audio_format"),
    "float": (wav_bytes(audio_format=3), "audio_format"),
    "extra-chunk": (wav_bytes(tail=b"LIST\x04\x00\x00\x00abcd"), "audio_format"),
    "wrong-data-size": (wav_bytes(data_size=32_002), "audio_format"),
    "wrong-riff-size": (wav_bytes(riff_size=1234), "audio_format"),
    "too-short": (wav_bytes(samples=3200), "audio_too_short"),
    "too-long": (wav_bytes(11.0), "audio_too_large"),
    "over-the-byte-cap": (wav_bytes(12.6), "audio_too_large"),
}


@pytest.mark.parametrize(
    "body, code",
    list(WAV_REFUSALS.values()),
    ids=list(WAV_REFUSALS),
)
def test_a_recording_that_is_not_the_exact_format_is_refused_unread_by_the_worker(
    machine, body, code
):
    assert_refused(machine, machine.post(body), code)


def test_the_limits_of_the_duration_are_accepted(machine):
    for seconds in (0.3, 10.0, 10.5):
        assert machine.post(wav_bytes(seconds)).status_code == 200, seconds


def test_a_good_recording_reaches_the_worker_byte_for_byte_with_the_site_cores(machine):
    audio = wav_bytes(1.5)
    assert machine.post(audio).status_code == 200
    assert machine.manager.calls == [(REQ, audio, "8-15")]


# --------------------------------------------------------------- the caps


def endless(size, count=200):
    """A body that would be ``count * size`` bytes: long enough to show a
    missing cap as a failed assertion, not as a test that never ends."""
    return itertools.islice(itertools.repeat(b"x" * size), count)


async def asgi_post(app, path, headers, chunks, query=b""):
    """Send a request straight to the ASGI app and count the body bytes the app
    pulled from the connection: ``(bytes read, status)``."""
    supply, read = iter(chunks), 0
    sent = []

    async def receive():
        nonlocal read
        chunk = next(supply, None)
        if chunk is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        read += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    return read, sent[0]["status"], b"".join(m.get("body", b"") for m in sent[1:])


def post_raw(machine, headers, chunks):
    app = machine.client.app
    return asyncio.run(
        asgi_post(
            app,
            f"{BASE}/transcribe",
            {"content-type": "audio/wav", **headers},
            chunks,
            f"request_id={REQ}".encode(),
        )
    )


def test_a_declared_length_over_the_cap_is_refused_before_a_byte_is_read(machine):
    read, status, body = post_raw(
        machine,
        {"content-length": str(settings.MAX_BYTES + 1)},
        endless(65536),
    )
    assert (read, status) == (0, 413)
    assert b"audio_too_large" in body
    assert machine.manager.calls == []


def test_a_stream_with_no_length_is_stopped_at_the_cap_not_read_to_the_end(machine):
    chunk = 65536
    read, status, body = post_raw(machine, {}, endless(chunk))
    assert status == 413 and b"audio_too_large" in body
    assert settings.MAX_BYTES < read <= settings.MAX_BYTES + chunk
    assert machine.manager.calls == []


def test_a_sender_that_understates_its_length_is_stopped_at_the_cap_too(machine):
    chunk = 65536
    read, status, _ = post_raw(machine, {"content-length": "1000"}, endless(chunk))
    assert status == 413 and read <= settings.MAX_BYTES + chunk


def test_a_body_of_exactly_the_cap_is_read_and_judged_as_audio(machine):
    exactly = b"x" * settings.MAX_BYTES
    assert_refused(machine, machine.post(exactly), "audio_format")
    assert_refused(
        machine,
        machine.post(exactly + b"x", query=f"?request_id={REQ}-2"),
        "audio_too_large",
    )


# --------------------------------------------------------------- the guards


def gated(machine, **status):
    machine.policy.doc = policy_status(**status)
    return machine.post()


@pytest.mark.parametrize("state", ["starting", "unknown"])
def test_a_policy_server_that_is_starting_or_unknown_refuses(machine, state):
    response = gated(machine, state=state)
    assert_refused(machine, response, "speech_policy_starting")
    assert response.json()["detail"]["reasons"] == ["policy_starting"]


@pytest.mark.parametrize(
    "state", ["idle", "ready", "failed", "dead", "stopping", "foreign_listener"]
)
def test_every_other_policy_state_lets_speech_run(machine, state):
    assert gated(machine, state=state).status_code == 200


def test_a_connected_client_refuses(machine):
    response = gated(machine, established=1)
    assert_refused(machine, response, "speech_client_connected")
    assert response.json()["detail"]["established"] == 1


@pytest.mark.parametrize("level", ["refuse", "unknown"])
def test_a_held_or_unreadable_arm_lock_refuses(machine, level):
    assert_refused(machine, gated(machine, arm=level), "speech_arm_in_use")


def test_a_missing_arm_lock_row_counts_as_in_use(machine):
    machine.policy.doc["checks"] = []
    assert_refused(machine, machine.post(), "speech_arm_in_use")


def test_too_little_free_gpu_memory_refuses_with_the_numbers(machine):
    need = settings.STT_VRAM_MIB + 1024  # the sample site's margin
    response = gated(machine, free_mib=need - 1)
    assert_refused(machine, response, "speech_gpu_insufficient")
    detail = response.json()["detail"]
    assert (detail["free_mib"], detail["required_mib"]) == (need - 1, need)
    assert (detail["reserved_mib"], detail["margin_mib"]) == (
        settings.STT_VRAM_MIB,
        1024,
    )
    assert str(need) in detail["message"]
    assert gated(machine, free_mib=need).status_code == 200


@pytest.mark.parametrize("gpu", ["unknown", "error"])
def test_an_unreadable_gpu_refuses_with_503(machine, gpu):
    assert_refused(machine, gated(machine, gpu=gpu), "speech_gpu_unreadable")


def test_a_gpu_part_without_a_number_is_unreadable(machine):
    machine.policy.doc["gpu"] = {"state": "ok", "free_mib": "lots"}
    assert_refused(machine, machine.post(), "speech_gpu_unreadable")
    machine.policy.doc.pop("gpu")
    assert_refused(machine, machine.post(), "speech_gpu_unreadable")


def test_a_loaded_worker_skips_the_memory_check_but_not_the_others(machine):
    machine.manager.view["state"] = "ready"
    assert gated(machine, free_mib=10).status_code == 200
    machine.manager.calls.clear()
    assert_refused(
        machine, gated(machine, free_mib=10, established=2), "speech_client_connected"
    )
    assert_refused(
        machine, gated(machine, free_mib=10, arm="refuse"), "speech_arm_in_use"
    )


def test_a_deploy_or_stop_in_progress_refuses_even_while_the_state_says_idle(machine):
    machine.policy.doc = policy_status(state="idle")
    machine.policy.doc["operation"] = {
        "action": "deploy",
        "request_id": "deploy-req-0001",
        "started_at": "2026-10-22T10:00:00+08:00",
    }
    response = machine.post()
    assert_refused(machine, response, "speech_policy_starting")
    assert response.json()["detail"]["reasons"] == ["policy_starting"]
    machine.policy.doc["operation"] = None
    assert machine.post().status_code == 200


def test_a_cold_start_asks_the_whole_gate_again_under_the_request(machine):
    # The status showed a loaded worker (no memory check), but it is gone by
    # the time the manager looks: the memory check runs before the start.
    machine.manager.view["state"] = "ready"
    machine.manager.cold = True
    machine.policy.doc = policy_status(free_mib=10)
    response = machine.post()
    assert (response.status_code, code_of(response)) == (409, "speech_gpu_insufficient")
    assert response.json()["detail"]["free_mib"] == 10
    assert machine.manager.asked == 1
    machine.policy.doc = policy_status()
    assert machine.post().status_code == 200 and machine.manager.asked == 2


def test_a_warm_worker_is_not_asked_the_memory_check(machine):
    machine.manager.view["state"] = "ready"
    machine.policy.doc = policy_status(free_mib=10)
    assert machine.post().status_code == 200 and machine.manager.asked == 0


def test_the_guard_closes_when_a_run_or_a_deploy_needs_the_card(machine):
    assert speech_api._guard_closed() is False
    # memory does not count: the worker the guard is asked about holds it
    machine.policy.doc = policy_status(free_mib=10)
    assert speech_api._guard_closed() is False
    for status in (
        policy_status(established=1),
        policy_status(arm="refuse"),
        policy_status(state="starting"),
        {**policy_status(), "operation": {"action": "deploy"}},
    ):
        machine.policy.doc = status
        assert speech_api._guard_closed() is True
    machine.policy.error = RuntimeError("unreadable")
    assert speech_api._guard_closed() is True


def test_the_real_manager_gets_the_guard_registered(monkeypatch):
    from levi.stt import manager as stt_manager

    real = stt_manager.Manager(watch=False)
    monkeypatch.setattr(stt_manager, "_MANAGER", [real])
    assert speech_api.manager() is real
    assert real._guard is speech_api._guard_closed


def test_a_status_that_cannot_be_read_counts_as_unknown(machine):
    machine.policy.error = RuntimeError("systemctl hung")
    assert_refused(machine, machine.post(), "speech_policy_starting")


def test_a_status_without_its_parts_refuses(machine):
    machine.policy.doc = {"state": "ready"}
    assert machine.post().status_code == 409


def test_the_first_reason_is_reported_and_all_are_listed(machine):
    response = gated(machine, established=1, arm="refuse", free_mib=100)
    assert code_of(response) == "speech_client_connected"
    assert response.json()["detail"]["reasons"] == [
        "client_connected",
        "arm_in_use",
        "gpu_insufficient",
    ]


def test_an_uninstalled_worker_is_reported_before_the_guards(machine):
    machine.manager.worker = settings.WorkerState(False, False, "worker_python_missing")
    machine.policy.doc = policy_status(state="starting")
    assert_refused(machine, machine.post(), "speech_not_installed")
    machine.manager.worker = settings.WorkerState(True, False, "model_missing")
    assert_refused(machine, machine.post(), "speech_model_missing")


def test_the_physical_gpu_lock_and_the_inference_guard_are_not_used():
    source = Path(speech_api.__file__).read_text()
    assert "inference.gpu" not in source and "gpu_lock" not in source
    assert "flock" not in source


# --------------------------------------------------------------- worker failures


@pytest.mark.parametrize(
    "error, status, code",
    [
        (SttError("busy", "x"), 429, "speech_busy"),
        (SttError("timeout", "x"), 504, "speech_timeout"),
        (SttError("failed", "x"), 502, "speech_failed"),
        (SttError("manifest_mismatch", "x"), 503, "speech_manifest_mismatch"),
        (SttError("model_missing", "x"), 503, "speech_model_missing"),
        (SttError("audio_invalid", "x"), 415, "audio_format"),
        (SttError("audio_too_long", "x"), 413, "audio_too_large"),
        (SttError("held", "x"), 409, "speech_policy_starting"),
        (SttError("something-new", "x"), 502, "speech_failed"),
    ],
)
def test_what_the_manager_refuses_with_maps_to_a_status(machine, error, status, code):
    machine.manager.error = error
    response = machine.post()
    assert (response.status_code, code_of(response)) == (status, code)
    assert machine.manager.calls  # it was asked


def test_a_busy_worker_says_why(machine):
    machine.manager.error = SttError("busy", "x")
    detail = machine.post().json()["detail"]
    assert detail["reason"] == "in_flight" and detail["limit"] == 10


# --------------------------------------------------------------- the rate limit


def test_the_eleventh_request_in_a_minute_is_refused_and_a_slot_frees_later(
    machine, monkeypatch
):
    now = [1000.0]
    monkeypatch.setattr(
        speech_api, "RATE", speech_api.RateLimit(10, 60.0, lambda: now[0])
    )
    for n in range(10):
        assert machine.post(query=f"?request_id=speech-req-{n:04d}").status_code == 200
        now[0] += 1.0
    response = machine.post()
    assert response.status_code == 429 and code_of(response) == "speech_busy"
    detail = response.json()["detail"]
    assert detail["reason"] == "rate_limit" and 49.0 <= detail["retry_after_s"] <= 51.0
    assert len(machine.manager.calls) == 10
    now[0] += 51.0
    assert machine.post().status_code == 200


def test_the_limit_counts_requests_that_fail_after_the_body_check_too(
    machine, monkeypatch
):
    now = [1000.0]
    monkeypatch.setattr(
        speech_api, "RATE", speech_api.RateLimit(10, 60.0, lambda: now[0])
    )
    for _ in range(10):
        assert machine.post(b"x" * 100).status_code == 415
    assert machine.post(b"x" * 100).status_code == 429


def test_the_rate_limiter_by_itself():
    now = [0.0]
    limiter = speech_api.RateLimit(2, 10.0, lambda: now[0])
    assert limiter.take() == 0.0 and limiter.take() == 0.0
    assert limiter.take() == pytest.approx(10.0)
    now[0] = 9.9
    assert limiter.take() == pytest.approx(0.1)
    now[0] = 10.0
    assert limiter.take() == 0.0  # the first hit has aged out
    limiter.reset()
    assert limiter.take() == 0.0 and limiter.take() == 0.0


# --------------------------------------------------------------- the answer

GENERATED_KEYS = {
    "status",
    "prompt",
    "frames",
    "episodes",
    "slots",
    "untranslated",
    "dataset_original",
    "repairs",
}


def test_a_transcription_answers_with_the_contract_shape(machine):
    response = machine.post()
    assert response.status_code == 200
    answer = response.json()
    assert re.fullmatch(r"[0-9a-f]{64}", answer["match"].pop("allowed_revision"))
    assert set(answer["match"].pop("generated")) == GENERATED_KEYS
    assert answer == {
        "schema": "levi.marvel0.speech.v1",
        "ok": True,
        "request_id": REQ,
        "heard": True,
        "reason": None,
        "transcript": HEARD,
        "language": "English",
        "match": {
            "tier": "A",
            "flag": None,
            "family": "all",
            "best": {
                "prompt": "pick screws of same size into the empty slot of screw box",
                "score": 100,
                "fixes": [],
                "differences": [],
            },
            "alternatives": [],
            "allowed": list(machine.matcher.calls[0][1]),
        },
        "engine": {"model": "qwen3-asr-1.7b-hf", "device": "cuda", "loaded_now": True},
        "timing_ms": {"load": 900, "infer": 104},
    }


def test_nothing_heard_is_a_normal_answer_without_a_match(machine):
    machine.manager.answer = transcript("", heard=False, reason="no_speech")
    body = machine.post().json()
    assert machine.post().status_code == 200
    assert (body["heard"], body["reason"], body["transcript"]) == (
        False,
        "no_speech",
        "",
    )
    assert (body["language"], body["match"]) == (None, None)
    assert machine.matcher.calls == []


def test_the_allowed_set_is_the_site_list_without_a_model(machine):
    machine.post()
    ((text, allowed, family),) = machine.matcher.calls
    site = marvel0_site.require()
    assert (text, allowed, family) == (HEARD, tuple(site.prompts.known), "all")
    assert machine.matcher.checked == [tuple(site.prompts.known)]


def test_a_model_of_the_unrestricted_family_gets_the_site_list(machine):
    machine.post(query=f"?request_id={REQ}&model_id=pi05_fr3_all_step49999")
    assert machine.matcher.calls[0][1:] == (
        tuple(marvel0_site.require().prompts.known),
        "all",
    )


def test_a_restricted_model_gets_only_the_two_it_was_trained_on(machine):
    response = machine.post(query=f"?request_id={REQ}&model_id=pi05_fr3_sp_v1")
    assert response.status_code == 200
    assert machine.matcher.calls[0][1:] == (
        marvel0_site.RESTRICTED_PROMPTS,
        "restricted",
    )
    assert machine.matcher.checked == [marvel0_site.RESTRICTED_PROMPTS]
    assert response.json()["match"]["family"] == "restricted"


def test_an_allowed_set_the_matcher_cannot_work_with_refuses_before_the_worker(machine):
    machine.matcher.problems = ["'x' is not in the lexicon"]
    response = machine.post()
    assert_refused(machine, response, "speech_allowed_set_invalid")
    assert "lexicon" not in response.text, "the matcher's sentences stay in the log"


def test_a_matcher_that_raises_is_a_502_and_its_message_is_not_shown(machine):
    machine.matcher.error = ValueError(f"cannot read {HEARD}")
    response = machine.post()
    assert response.status_code == 502 and code_of(response) == "speech_failed"
    assert HEARD not in response.text


def test_the_page_can_read_the_state_while_nothing_else_runs(machine):
    assert machine.client.get(BASE).status_code == 200
    assert machine.manager.calls == []


# --------------------------------------------------------------- GET speech


def view(machine):
    response = machine.client.get(BASE)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_status_answers_with_the_contract_shape(machine):
    answer = view(machine)
    assert re.fullmatch(r"[0-9a-f]{64}", answer.pop("allowed_revision"))
    assert answer == {
        "schema": "levi.marvel0.speech.status.v1",
        "state": "off",
        "reason": None,
        "installed": {"worker": True, "model": True},
        "model": "qwen3-asr-1.7b-hf",
        "device": "cuda",
        "limits": {"max_seconds": 10, "max_bytes": 400000},
        "idle_seconds": 120,
        "gate": {"allowed": True, "reasons": []},
    }


@pytest.mark.parametrize("state", ["off", "loading", "ready", "busy"])
def test_the_status_shows_the_workers_state(machine, state):
    machine.manager.view["state"] = state
    assert view(machine)["state"] == state


def test_a_failed_worker_shows_its_reason(machine):
    machine.manager.view.update(
        state="failed",
        reason={
            "code": "worker_exited",
            "text": "The speech worker stopped unexpectedly",
        },
    )
    shown = view(machine)
    assert shown["state"] == "failed" and shown["reason"]["code"] == "worker_exited"


def test_an_uninstalled_worker_is_unavailable_with_the_reason(machine):
    machine.manager.worker = settings.WorkerState(False, False, "worker_source_missing")
    shown = view(machine)
    assert shown["state"] == "unavailable" and shown["installed"] == {
        "worker": False,
        "model": False,
    }
    assert shown["reason"]["code"] == "speech_not_installed"
    machine.manager.worker = settings.WorkerState(True, False, "model_missing")
    shown = view(machine)
    assert shown["installed"] == {"worker": True, "model": False}
    assert shown["reason"]["code"] == "speech_model_missing"


def test_the_gate_lists_every_reason_and_never_starts_the_worker(machine):
    machine.policy.doc = policy_status(established=1, arm="unknown", free_mib=10)
    shown = view(machine)
    assert shown["gate"] == {
        "allowed": False,
        "reasons": ["client_connected", "arm_in_use", "gpu_insufficient"],
    }
    assert machine.manager.calls == [] and machine.manager.released == 0


def test_the_gate_skips_the_memory_check_for_a_loaded_worker(machine):
    machine.policy.doc = policy_status(free_mib=10)
    assert view(machine)["gate"]["reasons"] == ["gpu_insufficient"]
    machine.manager.view["state"] = "busy"
    assert view(machine)["gate"] == {"allowed": True, "reasons": []}


def test_the_status_says_when_the_allowed_set_cannot_be_used(machine):
    machine.matcher.problems = ["unknown word: aubergine"]
    shown = view(machine)
    assert shown["gate"] == {"allowed": False, "reasons": ["allowed_set_invalid"]}
    machine.matcher.problems = []
    assert view(machine)["gate"] == {"allowed": True, "reasons": []}


def test_the_status_checks_the_allowed_set_of_the_model_it_is_asked_for(machine):
    response = machine.client.get(f"{BASE}?model_id=pi05_fr3_sp_v1")
    assert response.status_code == 200
    assert machine.matcher.checked[-1] == tuple(models.RESTRICTED_PROMPTS)
    machine.client.get(BASE)
    assert len(machine.matcher.checked[-1]) > 2  # the site list
    # A model-specific status must not advertise a usable unknown model.
    shown = machine.client.get(f"{BASE}?model_id=nope")
    assert shown.status_code == 422
    assert code_of(shown) == "model_unknown"
    assert machine.client.get(f"{BASE}?model_id={'x' * 121}").status_code == 422


def test_an_unreadable_policy_status_closes_the_gate_but_still_answers(machine):
    machine.policy.error = RuntimeError("boom")
    assert view(machine)["gate"] == {"allowed": False, "reasons": ["policy_starting"]}


# --------------------------------------------------------------- POST match


def match(machine, body, **kwargs):
    return machine.client.post(f"{BASE}/match", json=body, **kwargs)


def test_an_edited_transcript_is_matched_without_the_worker(machine):
    response = match(machine, {"transcript": "stack the plates"})
    assert response.status_code == 200
    answer = response.json()
    assert answer["schema"] == "levi.marvel0.speech.match.v1"
    assert answer["match"]["family"] == "all"
    assert machine.matcher.calls[0][0] == "stack the plates"
    assert machine.manager.calls == [] and machine.manager.released == 0


def test_matching_takes_the_restricted_set_for_a_restricted_model(machine):
    response = match(machine, {"transcript": "x", "model_id": "pi05_fr3_sp_v1"})
    assert response.json()["match"]["family"] == "restricted"
    assert machine.matcher.calls[0][1] == marvel0_site.RESTRICTED_PROMPTS


def test_matching_with_an_unknown_model_is_a_422(machine):
    response = match(machine, {"transcript": "x", "model_id": "nope"})
    assert response.status_code == 422 and code_of(response) == "model_unknown"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"transcript": ""},
        {"transcript": "   "},
        {"transcript": 5},
        {"transcript": None},
        {"transcript": "x" * 501},
        {"transcript": "x", "extra": 1},
        {"transcript": "x", "model_id": 3},
        {"transcript": "x", "model_id": "m" * 121},
        ["transcript"],
        "transcript",
    ],
)
def test_a_match_body_that_is_not_the_closed_form_is_refused(machine, body):
    response = match(machine, body)
    assert response.status_code == 400 and code_of(response) == "speech_body_invalid"
    assert machine.matcher.calls == []


def test_a_match_body_that_is_not_json_is_refused_without_echoing_it(machine):
    response = machine.client.post(f"{BASE}/match", content=b"{not json, SECRETWORD")
    assert response.status_code == 400 and code_of(response) == "speech_body_invalid"
    assert "SECRETWORD" not in response.text


def test_a_large_match_body_is_stopped_at_its_cap(machine):
    read, status, _ = asyncio.run(
        asgi_post(
            machine.client.app,
            f"{BASE}/match",
            {"content-type": "application/json"},
            endless(4096),
        )
    )
    assert status == 400 and read <= speech_api.MATCH_MAX_BYTES + 4096


# --------------------------------------------------------------- POST release


def test_release_stops_the_worker_and_answers_off_every_time(machine):
    for n in (1, 2):
        response = machine.client.post(f"{BASE}/release")
        assert response.status_code == 200
        assert response.json() == {"state": "off", "released": True}
        assert machine.manager.released == n


# --------------------------------------------------------------- no paths


def test_no_answer_names_a_path_on_the_machine(machine):
    answers = [
        machine.client.get(BASE),
        machine.post(),
        machine.post(b"junk"),
        match(machine, {"transcript": "x"}),
        machine.client.post(f"{BASE}/release"),
    ]
    machine.manager.worker = settings.WorkerState(False, False, "worker_source_missing")
    answers += [machine.client.get(BASE), machine.post(query="?request_id=bad")]
    for response in answers:
        assert not PATH_IN_TEXT.search(response.text), response.text
        assert str(machine.tmp_path) not in response.text


def test_allowed_revision_is_shared_by_status_and_match_and_changes_with_exact_list(
    machine, monkeypatch
):
    first = machine.client.get(BASE).json()["allowed_revision"]
    matched = machine.client.post(f"{BASE}/match", json={"transcript": HEARD}).json()
    assert matched["match"]["allowed_revision"] == first
    assert len(first) == 64
    original = speech_api._allowed

    def changed(site, model_id):
        allowed, family = original(site, model_id)
        return (allowed[0] + " ", *allowed[1:]), family

    monkeypatch.setattr(speech_api, "_allowed", changed)
    second = machine.client.get(BASE).json()["allowed_revision"]
    assert second != first
    matched = machine.client.post(f"{BASE}/match", json={"transcript": HEARD}).json()
    assert matched["match"]["allowed_revision"] == second


def test_status_revision_is_model_scoped_and_unknown_model_is_rejected(machine):
    all_revision = machine.client.get(f"{BASE}?model_id=pi05_fr3_all_step49999").json()[
        "allowed_revision"
    ]
    restricted = machine.client.get(f"{BASE}?model_id=pi05_fr3_sp_v1").json()[
        "allowed_revision"
    ]
    assert all_revision != restricted
    response = machine.client.get(f"{BASE}?model_id=not-offered")
    assert response.status_code == 422
    assert code_of(response) == "model_unknown"
    assert machine.manager.calls == []
