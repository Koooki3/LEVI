"""The speech routes over the real worker manager and the stand-in worker of
``tests/stt/fake_worker.py`` (real processes and pipes, no model): the whole
path from the HTTP request to the pipe and back, the status that never starts
anything, and the promise that neither audio nor a transcript is written
anywhere. The real matcher is used where a test says so."""

import hashlib
import importlib.util
import logging
import tempfile
from pathlib import Path

import pytest
from test_marvel0_speech_route import (  # noqa: F401 - helpers and fixtures
    BASE,
    REQ,
    TRANSCRIBE,
    Matcher,
    build_machine,
    code_of,
    no_token,
    policy_status,
    wav_bytes,
)

from levi import children
from levi.marvel0 import models, speech_api
from levi.marvel0 import site as marvel0_site
from levi.stt import settings
from levi.stt.manager import Manager

_spec = importlib.util.spec_from_file_location(
    "stt_fake_worker", Path(__file__).resolve().parents[1] / "stt" / "fake_worker.py"
)
fake_worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_worker)

SENTINEL = "ZEBRAQUARTZ"
AUDIO_MARK = b"\xa5\x5a\x3c\xc3\x96\x69\x0f\xf0" * 8


def marked_wav(seconds=1.0):
    """A valid recording whose samples are a recognisable pattern."""
    plain = wav_bytes(seconds)
    body = (AUDIO_MARK * (len(plain) // len(AUDIO_MARK) + 1))[: len(plain) - 44]
    return plain[:44] + body


@pytest.fixture
def real(tmp_path, monkeypatch):
    """A route over a real ``Manager`` whose worker is the stand-in; ``script``
    is set before the first request."""
    # Anything the code might write as a temporary file lands under tmp_path.
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    state = {"script": []}

    def command(cpu_affinity):
        return fake_worker.command(
            tmp_path, state["script"], record=tmp_path / "record.jsonl"
        )(cpu_affinity)

    manager = Manager(
        command=command,
        installed=lambda: settings.WorkerState(True, True),
        cwd=tmp_path,
        log_dir=tmp_path / "workspace" / "stt",
        children_state=tmp_path / "workspace" / "processes.json",
        request_timeout=3,
        start_timeout=10,
        idle_seconds=120,
        watch=True,
    )
    machine = build_machine(tmp_path, monkeypatch, manager, Matcher())
    machine.state = state
    machine.record = tmp_path / "record.jsonl"
    yield machine
    manager.stop()
    rows = children.listed(state=tmp_path / "workspace" / "processes.json")
    assert [r for r in rows if r["running"]] == []
    marvel0_site.clear_cache()
    models.cache_clear()


def worker_starts(machine):
    return [r for r in fake_worker.records(machine.record) if "pid" in r]


def test_a_recording_goes_through_the_real_manager_and_back(real):
    real.state["script"] = [{"text": "pick the eggplant in the blue plate"}, {}]
    audio = wav_bytes(1.2)
    first = real.post(audio)
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["heard"], body["transcript"]) == (
        True,
        "pick the eggplant in the blue plate",
    )
    assert body["engine"]["loaded_now"] is True
    assert body["timing_ms"]["load"] > 0
    second = real.post(audio, query="?request_id=speech-req-0002")
    assert second.json()["engine"]["loaded_now"] is False
    assert len(worker_starts(real)) == 1
    sent = [r for r in fake_worker.records(real.record) if r.get("op") == "request"]
    assert [r["bytes"] for r in sent] == [len(audio)] * 2
    assert real.matcher.calls[0][2] == "all"


def test_the_status_never_starts_the_worker_and_release_ends_it(real):
    for _ in range(3):
        shown = real.client.get(BASE).json()
        assert shown["state"] == "off" and shown["gate"]["allowed"] is True
    assert fake_worker.records(real.record) == []
    assert not (real.tmp_path / "workspace" / "stt").exists(), "nothing was created"
    assert real.post().status_code == 200
    assert real.client.get(BASE).json()["state"] == "ready"
    pid = real.manager.worker_pid()
    assert real.client.post(f"{BASE}/release").json() == {
        "state": "off",
        "released": True,
    }
    assert (
        real.manager.worker_pid() is None
        and real.client.get(BASE).json()["state"] == "off"
    )
    assert real.client.post(f"{BASE}/release").json()["released"] is True
    assert pid is not None


def test_a_dead_worker_is_shown_failed_and_the_next_request_recovers(real):
    real.state["script"] = [{"do": "crash"}, {"text": "back again"}]
    response = real.post()
    assert response.status_code == 502 and code_of(response) == "speech_failed"
    shown = real.client.get(BASE).json()
    assert shown["state"] == "failed" and shown["reason"]["code"] == "worker_exited"
    assert real.post().json()["transcript"] == "back again"
    assert real.client.get(BASE).json()["state"] == "ready"


def test_a_worker_that_does_not_answer_is_a_504_and_is_killed(real):
    real.manager._request_timeout = 0.7
    real.state["script"] = [{"do": "sleep", "seconds": 60, "grandchild": True}]
    response = real.post()
    assert response.status_code == 504 and code_of(response) == "speech_timeout"
    assert real.manager.worker_pid() is None


def test_with_the_real_matcher_the_site_list_and_the_restricted_pair_are_usable(
    real, monkeypatch
):
    matcher = pytest.importorskip("levi.marvel0.speech_match")
    monkeypatch.setattr(speech_api, "_matcher", lambda: matcher)
    site = marvel0_site.require()
    assert matcher.check_allowed_set(site.prompts.known) == []
    assert matcher.check_allowed_set(marvel0_site.RESTRICTED_PROMPTS) == []
    real.state["script"] = [
        {"text": "pick the eggplant in the blue plate"},
        {"text": "pick the eggplant in the blue plate"},
        {"text": "please stop"},
    ]
    shown = real.post().json()["match"]
    assert (shown["tier"], shown["best"]["prompt"]) == (
        "A",
        "pick the eggplant in the blue plate",
    )
    assert shown["family"] == "all"
    restricted = real.post(query="?request_id=speech-req-0002&model_id=pi05_fr3_sp_v1")
    assert restricted.json()["match"]["tier"] != "A", (
        "that sentence is not one of the two"
    )
    assert restricted.json()["match"]["allowed"] == list(
        marvel0_site.RESTRICTED_PROMPTS
    )
    vetoed = real.post(query="?request_id=speech-req-0003").json()["match"]
    assert (vetoed["tier"], vetoed["flag"], vetoed["best"]) == ("V", "stop", None)


# --------------------------------------------------------------- nothing is kept


def mixed_requests(real, caplog):
    """Twenty-odd requests of every kind, each carrying the sentinel somewhere a
    leak could come from: the transcript, the worker's own words, a broken
    line of the worker, the text of an edited transcript."""
    real.state["script"] = [
        {"text": f"pick the {SENTINEL} in the blue plate"},
        {"text": f"stack the {SENTINEL} plates", "language": "Chinese"},
        {"do": "heard_false", "reason": "no_speech"},
        {"do": "error", "code": "audio_invalid", "message": f"bad {SENTINEL}"},
        {"text": f"{SENTINEL} one"},
        {"do": "error", "code": "internal", "message": f"boom {SENTINEL}"},
        {"text": f"{SENTINEL} two"},
        {"do": "garbage", "line": f"{SENTINEL} is not json"},
        {"text": f"{SENTINEL} three"},
        {"do": "crash"},
        {"text": f"{SENTINEL} four"},
        {"do": "heard_false", "reason": "language"},
        {"do": "sleep", "seconds": 60},
        {"text": f"{SENTINEL} five"},
    ]
    real.manager._request_timeout = 1.0
    answers, counter = [], iter(range(1, 100))

    def post(body=None, **kwargs):
        query = kwargs.pop("query", f"?request_id=speech-mix-{next(counter):04d}")
        response = real.post(
            marked_wav() if body is None else body, query=query, **kwargs
        )
        answers.append(response)
        return response

    for _ in range(14):  # one per script step
        post()
    post(b"RIFF" + b"x" * 500)  # not a WAV
    post(query="?request_id=bad")  # bad id
    post(headers={"content-type": "audio/webm"})  # wrong type
    post(b"x" * (settings.MAX_BYTES + 5))  # over the cap
    real.policy.doc = policy_status(established=1)
    post()  # a client is connected
    real.policy.doc = policy_status()
    for text in (f"{SENTINEL} plates", {"transcript": SENTINEL * 200}):
        answers.append(
            real.client.post(
                f"{BASE}/match",
                json={"transcript": text} if isinstance(text, str) else text,
            )
        )
    answers.append(real.client.post(f"{BASE}/match", content=f"{{{SENTINEL}".encode()))
    return answers


def files_below(root):
    for path in Path(root).rglob("*"):
        if path.is_file():
            yield path


def test_neither_a_transcript_nor_audio_is_written_anywhere(real, caplog, monkeypatch):
    monkeypatch.setattr(speech_api, "RATE", speech_api.RateLimit(1000, 60.0))
    caplog.set_level(logging.DEBUG)
    answers = mixed_requests(real, caplog)
    assert len(answers) >= 20
    statuses = sorted({a.status_code for a in answers})
    assert {200, 400, 409, 413, 415, 502, 504} <= set(statuses), statuses

    # The page gets the transcript of the requests that were heard, and only those.
    leaking = [a for a in answers if a.status_code != 200 and SENTINEL in a.text]
    assert leaking == [], "a refusal echoed the sentinel"
    assert sum(SENTINEL in a.text for a in answers) >= 5

    # No log line, at any level, from any logger.
    assert SENTINEL not in caplog.text
    for record in caplog.records:
        assert SENTINEL not in record.getMessage()
        assert SENTINEL not in str(record.args) + str(record.exc_text)
    assert any(r.name == "levi.marvel0.speech" for r in caplog.records), "audit lines"

    # No file of the workspace, the temporary folder or the worker's log.
    real.manager.stop()
    # (the stand-in's script is the test's own input and holds the sentinel)
    seen = [p for p in files_below(real.tmp_path) if "worker-script" not in p.name]
    assert any(p.name == "worker.log" for p in seen)
    for path in seen:
        data = path.read_bytes()
        assert SENTINEL.encode() not in data, path.name
        assert AUDIO_MARK not in data, path.name
    assert not list(files_below(real.tmp_path / "tmp")), "no temporary file at all"


def test_the_audit_line_has_only_ids_codes_counts_and_hash_prefixes(real, caplog):
    caplog.set_level(logging.INFO, logger="levi.marvel0.speech")
    real.state["script"] = [{"text": f"pick the {SENTINEL}"}]
    audio = marked_wav()
    real.post(audio)
    (record,) = [r for r in caplog.records if r.name == "levi.marvel0.speech"]
    message = record.getMessage()
    assert message.startswith(f"speech request_id={REQ} outcome=heard ")
    assert f"audio_bytes={len(audio)}" in message and "audio_mac=" in message
    assert "chars=" in message and "text_mac=" in message
    assert SENTINEL not in message
    # The fingerprint is keyed per process: hashing the sentence that was said
    # (the closed set of a site's instructions) does not find it in the log.
    said = f"pick the {SENTINEL}".encode()
    tag = message.split("text_mac=")[1].split()[0]
    assert len(tag) == 8 and tag == speech_api._fingerprint(said)
    assert hashlib.sha256(said).hexdigest()[:8] not in message
    assert hashlib.sha256(audio).hexdigest()[:8] not in message
