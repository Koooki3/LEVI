"""The speech worker's pure pieces (``integrations/stt``): strict WAV check,
model manifest, gate pipeline, serve loop and installer.

Everything runs in this process with LEVI's own Python: no Torch, no GPU, no
model. The recogniser and the VAD are small fakes injected into the real gate
code; the serve loop reads from and writes to in-memory streams.
"""

from __future__ import annotations

import io
import json
import struct
import sys
import types
from pathlib import Path

import numpy as np
import pytest

WORKER_SRC = Path(__file__).resolve().parents[2] / "integrations" / "stt"
SR = 16000


@pytest.fixture(scope="module", autouse=True)
def worker_on_path():
    sys.path.insert(0, str(WORKER_SRC))
    yield
    sys.path.remove(str(WORKER_SRC))
    for name in [n for n in sys.modules if n.startswith("levi_stt_worker")]:
        del sys.modules[name]


def wav_bytes(seconds: float = 1.0, **header) -> bytes:
    """A canonical clip; ``header`` overrides single header fields."""
    pcm = b"\x01\x00" * round(seconds * SR)
    fields = {
        "riff_size": 36 + len(pcm),
        "fmt_size": 16,
        "tag": 1,
        "channels": 1,
        "rate": SR,
        "byte_rate": SR * 2,
        "block": 2,
        "bits": 16,
        "data_size": len(pcm),
        "riff": b"RIFF",
        "wave": b"WAVE",
        "fmt": b"fmt ",
        "data": b"data",
    }
    fields.update(header)
    return (
        fields["riff"]
        + struct.pack("<I", fields["riff_size"])
        + fields["wave"]
        + fields["fmt"]
        + struct.pack(
            "<IHHIIHH",
            fields["fmt_size"],
            fields["tag"],
            fields["channels"],
            fields["rate"],
            fields["byte_rate"],
            fields["block"],
            fields["bits"],
        )
        + fields["data"]
        + struct.pack("<I", fields["data_size"])
        + pcm
    )


# ---------------------------------------------------------------------- wav


def test_wav_accepts_the_canonical_clip_and_measures_it():
    from levi_stt_worker.wav import parse_wav

    clip = parse_wav(wav_bytes(1.5), 10)
    assert clip.seconds == pytest.approx(1.5)
    assert len(clip.pcm) == 48000


@pytest.mark.parametrize(
    ("label", "header"),
    [
        ("riff magic", {"riff": b"RIFX"}),
        ("wave magic", {"wave": b"WAVX"}),
        ("fmt magic", {"fmt": b"fmtx"}),
        ("data magic", {"data": b"LIST"}),
        ("fmt chunk size", {"fmt_size": 18}),
        ("float samples", {"tag": 3}),
        ("stereo", {"channels": 2}),
        ("8 kHz", {"rate": 8000}),
        ("48 kHz", {"rate": 48000}),
        ("8 bit", {"bits": 8}),
        ("byte rate", {"byte_rate": 16000}),
        ("block align", {"block": 4}),
        ("riff size too small", {"riff_size": 100}),
        ("riff size too large", {"riff_size": 36 + 32000 + 2}),
        ("data size too small", {"data_size": 31998}),
        ("data size too large", {"data_size": 32002}),
    ],
)
def test_wav_rejects_every_header_deviation(label, header):
    from levi_stt_worker.errors import RequestError
    from levi_stt_worker.wav import parse_wav

    with pytest.raises(RequestError) as caught:
        parse_wav(wav_bytes(1.0, **header), 10)
    assert caught.value.code == "audio_invalid", label


def test_wav_rejects_truncated_extra_chunk_and_odd_length():
    from levi_stt_worker.errors import RequestError
    from levi_stt_worker.wav import parse_wav

    good = wav_bytes(1.0)
    for data in (good[:43], b"", good[:20], good + b"\x00", good[:-1]):
        with pytest.raises(RequestError) as caught:
            parse_wav(data, 10)
        assert caught.value.code == "audio_invalid"
    # a LIST chunk between fmt and data is the "extra chunk" the contract forbids
    extra = good[:36] + b"LIST" + struct.pack("<I", 4) + b"abcd" + good[36:]
    with pytest.raises(RequestError):
        parse_wav(extra, 10)


def test_wav_rejects_an_odd_number_of_data_bytes_even_when_the_sizes_agree():
    from levi_stt_worker.errors import RequestError
    from levi_stt_worker.wav import parse_wav

    body = wav_bytes(1.0)[:-1]
    odd = (
        body[:4]
        + struct.pack("<I", len(body) - 8)
        + body[8:40]
        + struct.pack("<I", len(body) - 44)
        + body[44:]
    )
    with pytest.raises(RequestError) as caught:
        parse_wav(odd, 10)
    assert caught.value.code == "audio_invalid"


def test_wav_duration_limits():
    from levi_stt_worker.errors import RequestError
    from levi_stt_worker.wav import parse_wav

    assert parse_wav(wav_bytes(0.3), 10).seconds == pytest.approx(0.3)
    with pytest.raises(RequestError) as short:
        parse_wav(wav_bytes(0.29), 10)
    assert short.value.code == "audio_invalid"
    assert parse_wav(wav_bytes(10.5), 10).seconds == pytest.approx(10.5)
    with pytest.raises(RequestError) as long:
        parse_wav(wav_bytes(10.51), 10)
    assert long.value.code == "too_long"
    # the limit follows --max-seconds (plus the 0.5 s tail), not a constant
    assert parse_wav(wav_bytes(6.0), 5.5).seconds == pytest.approx(6.0)
    with pytest.raises(RequestError) as tight:
        parse_wav(wav_bytes(6.1), 5.5)
    assert tight.value.code == "too_long"


# ----------------------------------------------------------------- manifest


def make_model_dir(root: Path) -> Path:
    folder = root / "model"
    (folder / "sub").mkdir(parents=True)
    (folder / "config.json").write_text('{"a": 1}')
    (folder / "weights.bin").write_bytes(b"\x00" * 1000)
    (folder / "sub" / "tok.json").write_text("{}")
    return folder


def test_manifest_round_trip_uses_the_sha256sum_format(tmp_path):
    import hashlib

    from levi_stt_worker.manifest import verify_manifest, write_manifest

    folder = make_model_dir(tmp_path)
    written = write_manifest(folder)
    assert sorted(written) == ["config.json", "sub/tok.json", "weights.bin"]
    lines = (folder / "MANIFEST.sha256").read_text().splitlines()
    digest = hashlib.sha256(b"\x00" * 1000).hexdigest()
    assert f"{digest}  weights.bin" in lines
    assert verify_manifest(folder) == written


@pytest.mark.parametrize(
    "tamper",
    [
        lambda f: (f / "weights.bin").write_bytes(b"\x00" * 999 + b"\x01"),
        lambda f: (f / "config.json").write_text('{"a": 2}'),
        lambda f: (f / "sub" / "tok.json").unlink(),
        lambda f: (f / "extra.py").write_text("import os"),
        lambda f: (f / "sub" / "more.json").write_text("{}"),
    ],
    ids=[
        "weights changed",
        "config changed",
        "listed file missing",
        "unlisted file",
        "unlisted file in subfolder",
    ],
)
def test_manifest_mismatch_when_the_folder_differs(tmp_path, tamper):
    from levi_stt_worker.errors import StartupError
    from levi_stt_worker.manifest import verify_manifest, write_manifest

    folder = make_model_dir(tmp_path)
    write_manifest(folder)
    tamper(folder)
    with pytest.raises(StartupError) as caught:
        verify_manifest(folder)
    assert caught.value.code == "manifest_mismatch"
    assert caught.value.exit_code != 0
    assert str(tmp_path) not in caught.value.message  # no filesystem path in an event


def test_manifest_refuses_symlinks_even_to_matching_content(tmp_path):
    from levi_stt_worker.errors import StartupError
    from levi_stt_worker.manifest import verify_manifest, write_manifest

    folder = make_model_dir(tmp_path)
    write_manifest(folder)
    elsewhere = tmp_path / "elsewhere.bin"
    elsewhere.write_bytes(b"\x00" * 1000)
    (folder / "weights.bin").unlink()
    (folder / "weights.bin").symlink_to(elsewhere)
    with pytest.raises(StartupError) as caught:
        verify_manifest(folder)
    assert caught.value.code == "manifest_mismatch"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not a manifest line\n",
        "AB" * 32 + "  config.json\n",  # upper case hex
        "0" * 64 + " config.json\n",  # one space
        "0" * 64 + "  ../escape\n",
        "0" * 64 + "  /abs/path\n",
        "0" * 64 + "  a//b\n",
        "0" * 64 + "  config.json\n" + "1" * 64 + "  config.json\n",  # duplicate
    ],
)
def test_manifest_malformed_lines_are_a_mismatch(tmp_path, text):
    from levi_stt_worker.errors import StartupError
    from levi_stt_worker.manifest import verify_manifest

    folder = make_model_dir(tmp_path)
    (folder / "MANIFEST.sha256").write_text(text)
    with pytest.raises(StartupError) as caught:
        verify_manifest(folder)
    assert caught.value.code == "manifest_mismatch"


def test_manifest_missing_file_and_missing_folder_have_their_own_codes(tmp_path):
    from levi_stt_worker.errors import StartupError
    from levi_stt_worker.manifest import verify_manifest

    folder = make_model_dir(tmp_path)
    with pytest.raises(StartupError) as no_manifest:
        verify_manifest(folder)
    assert no_manifest.value.code == "manifest_mismatch"
    with pytest.raises(StartupError) as no_folder:
        verify_manifest(tmp_path / "absent")
    assert no_folder.value.code == "model_missing"


# -------------------------------------------------------------------- gates


class FakeVad:
    def __init__(self, spans):
        self.spans = spans
        self.calls = 0

    def segments(self, samples):
        self.calls += 1
        return self.spans


class FakeRecogniser:
    def __init__(self, recognition=None):
        from levi_stt_worker.gates import Recognition

        self.recognition = recognition or Recognition(
            "English", "pick the eggplant", -0.05
        )
        self.heard: list[np.ndarray] = []

    def recognise(self, samples):
        self.heard.append(samples)
        return self.recognition


def clip(seconds: float = 10.0) -> np.ndarray:
    return np.arange(round(seconds * SR), dtype=np.float32)


def gate(spans, recognition=None, seconds=10.0, clock=None):
    from levi_stt_worker import gates

    recogniser = FakeRecogniser(recognition)
    kwargs = {"clock": clock} if clock else {}
    return gates.run_gates(
        clip(seconds), FakeVad(spans), recogniser, **kwargs
    ), recogniser


def test_gate_less_than_a_quarter_second_of_speech_never_reaches_the_model():
    body, recogniser = gate([(SR, SR + 3999)])  # 0.2499 s
    assert body == {"heard": False, "reason": "no_speech"}
    assert recogniser.heard == []
    body, recogniser = gate([])
    assert body == {"heard": False, "reason": "no_speech"}
    assert recogniser.heard == []


def test_gate_a_quarter_second_of_speech_is_enough_and_spans_add_up():
    body, _ = gate([(SR, SR + 4000)])  # exactly 0.25 s
    assert body["heard"] is True
    assert body["speech_s"] == 0.25
    body, _ = gate([(SR, SR + 2000), (3 * SR, 3 * SR + 2000)])  # 0.125 + 0.125 s
    assert body["heard"] is True
    assert body["speech_s"] == 0.25


def test_gate_sends_the_speech_with_one_second_of_context_not_the_whole_clip():
    # speech 4.0-5.0 s of a 10 s clip: the model gets 3.0-6.0 s
    _, recogniser = gate([(4 * SR, 5 * SR)])
    (heard,) = recogniser.heard
    assert heard[0] == 3 * SR and len(heard) == 3 * SR
    # context is clipped at the clip's edges
    _, recogniser = gate([(SR // 2, SR), (9 * SR, 10 * SR)])
    assert len(recogniser.heard[0]) == 10 * SR


def test_gate_a_heard_result_has_the_contract_fields():
    ticks = iter([0.0, 0.0081, 0.0081, 0.1121])
    body, _ = gate([(SR, 2 * SR)], clock=lambda: next(ticks))
    assert body == {
        "heard": True,
        "language": "English",
        "text": "pick the eggplant",
        "avg_logprob": -0.05,
        "speech_s": 1.0,
        "timing_ms": {"vad": 8, "infer": 104},
    }


@pytest.mark.parametrize(
    ("language", "text"),
    [(None, "hello"), ("English", ""), ("English", "  \n "), (None, "")],
)
def test_gate_language_none_or_empty_text_is_empty(language, text):
    from levi_stt_worker.gates import Recognition

    body, _ = gate([(SR, 2 * SR)], Recognition(language, text, -0.01))
    assert body == {"heard": False, "reason": "empty"}


@pytest.mark.parametrize("language", ["Japanese", "Finnish", "Cantonese", "", "En"])
def test_gate_only_english_and_chinese_pass(language):
    from levi_stt_worker.gates import Recognition

    body, _ = gate([(SR, 2 * SR)], Recognition(language, "konnichiwa", -0.01))
    assert body == {"heard": False, "reason": "language"}


def test_gate_language_names_are_canonicalised():
    from levi_stt_worker.gates import Recognition

    body, _ = gate([(SR, 2 * SR)], Recognition("chinese", "拿起茄子", -0.01))
    assert body["language"] == "Chinese"


@pytest.mark.parametrize(
    ("score", "capped"),
    [
        (-0.5001, False),
        (-3.0, False),
        (None, False),
        (float("nan"), False),
        (float("-inf"), False),
        (-0.01, True),
    ],
)
def test_gate_low_confidence_rejects(score, capped):
    from levi_stt_worker.gates import Recognition

    body, _ = gate(
        [(SR, 2 * SR)], Recognition("English", "pick the eggplant", score, capped)
    )
    assert body == {"heard": False, "reason": "low_confidence"}


def test_gate_the_confidence_boundary_is_inclusive():
    from levi_stt_worker.gates import Recognition

    body, _ = gate([(SR, 2 * SR)], Recognition("English", "pick", -0.5))
    assert body["heard"] is True


def test_gate_text_is_one_clean_line_and_zero_is_not_negative_zero():
    from levi_stt_worker.gates import Recognition

    body, _ = gate(
        [(SR, 2 * SR)], Recognition("English", " pick\n the   eggplant\t", -0.0)
    )
    assert body["text"] == "pick the eggplant"
    assert json.dumps(body["avg_logprob"]) == "0.0"


# ------------------------------------------------------- model-side helpers


def test_text_logprob_averages_only_the_text_tokens():
    from levi_stt_worker.qwen import text_logprob

    stop = {1, 2}
    # language tag tokens (scores -9) before <asr_text> (id 9) and an end token with a bad score are ignored
    tokens = [5, 6, 9, 10, 11, 1, 0]
    scores = [-9.0, -9.0, -9.0, -0.2, -0.4, -8.0, -8.0]
    assert text_logprob(tokens, scores, 9, stop) == pytest.approx(-0.3)
    # a model that wrote no tag: every token before the end token counts
    assert text_logprob([10, 11, 2], [-0.1, -0.3, -7.0], 9, stop) == pytest.approx(-0.2)
    # nothing between the tag and the end token
    assert text_logprob([5, 6, 9, 1], [-1.0, -1.0, -1.0, -1.0], 9, stop) is None
    # no end token at all (capped): the rest of the sequence counts
    assert text_logprob([9, 10, 11], [-5.0, -0.5, -0.5], 9, stop) == pytest.approx(-0.5)


def test_until_stop_and_the_token_limit():
    from levi_stt_worker.qwen import hit_token_limit, until_stop

    stop = {1, 2}
    assert until_stop([7, 8, 2, 9, 1], stop) == [7, 8]
    assert until_stop([7, 8], stop) == [7, 8]
    assert hit_token_limit([7] * 64, stop, 64) is True
    assert (
        hit_token_limit([7] * 63 + [2], stop, 64) is False
    )  # ended on its own at the limit
    assert hit_token_limit([7] * 63, stop, 64) is False
    assert hit_token_limit([7] * 10 + [1] + [7] * 60, stop, 64) is False


# ------------------------------------------------- model loading (stub Torch)


class StubModel:
    """Stands in for the transformers model: only where its weights are."""

    dtype = "bf16"
    generation_config = types.SimpleNamespace(eos_token_id=[1, 2])

    def __init__(self, parameter_devices):
        self._devices = list(parameter_devices)
        # like transformers: the device of the first parameter
        self.device = types.SimpleNamespace(
            type=self._devices[0] if self._devices else "cpu"
        )

    def parameters(self):
        return (
            types.SimpleNamespace(device=types.SimpleNamespace(type=d))
            for d in self._devices
        )

    def eval(self):
        return self


def stub_stack(monkeypatch, *, cuda=True, devices=("cuda", "cuda")):
    """Fake ``torch`` and ``transformers`` modules so QwenAsrProvider._load runs
    in LEVI's own Python. Returns the kwargs the model was loaded with."""
    loaded: dict = {}
    tokenizer = types.SimpleNamespace(
        convert_tokens_to_ids=lambda token: 9, eos_token_id=1, unk_token_id=0
    )

    class Model:
        @staticmethod
        def from_pretrained(path, **kwargs):
            loaded.update(kwargs)
            return StubModel(devices)

    class Processor:
        @staticmethod
        def from_pretrained(path, **kwargs):
            return types.SimpleNamespace(tokenizer=tokenizer)

    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: cuda)
    torch.set_num_threads = lambda count: None
    torch.bfloat16 = "bf16"
    hf_logging = types.ModuleType("transformers.utils.logging")
    hf_logging.disable_progress_bar = lambda: None
    hf_logging.set_verbosity_error = lambda: None
    utils = types.ModuleType("transformers.utils")
    utils.logging = hf_logging
    transformers = types.ModuleType("transformers")
    transformers.utils = utils
    transformers.AutoProcessor = Processor
    transformers.Qwen3ASRForConditionalGeneration = Model
    for name, module in (
        ("torch", torch),
        ("transformers", transformers),
        ("transformers.utils", utils),
        ("transformers.utils.logging", hf_logging),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    from levi_stt_worker import qwen

    monkeypatch.setattr(qwen, "SileroDetector", lambda: object())
    return loaded


def test_load_refuses_a_machine_without_cuda(monkeypatch):
    from levi_stt_worker.common import EXIT_STARTUP
    from levi_stt_worker.errors import StartupError
    from levi_stt_worker.qwen import QwenAsrProvider

    stub_stack(monkeypatch, cuda=False)
    with pytest.raises(StartupError) as caught:
        QwenAsrProvider(Path("model"), "cuda")._load()
    assert caught.value.exit_code == EXIT_STARTUP and "CUDA" in caught.value.message


@pytest.mark.parametrize(
    ("devices", "reported"),
    [
        (("cpu", "cpu"), "cpu"),
        (("cuda", "cpu"), "mixed"),  # an offloaded layer: model.device alone says cuda
        (("meta",), "meta"),
        ((), "none"),
    ],
)
def test_load_refuses_a_model_that_is_not_fully_on_the_gpu(
    monkeypatch, devices, reported
):
    from levi_stt_worker.common import EXIT_STARTUP
    from levi_stt_worker.errors import StartupError
    from levi_stt_worker.qwen import QwenAsrProvider

    stub_stack(monkeypatch, devices=devices)
    with pytest.raises(StartupError) as caught:
        QwenAsrProvider(Path("model"), "cuda")._load()
    assert caught.value.exit_code == EXIT_STARTUP
    assert caught.value.code == "internal" and reported in caught.value.message


def test_load_asks_for_the_gpu_and_reports_the_device_the_model_really_sits_on(
    monkeypatch,
):
    from levi_stt_worker import qwen

    loaded = stub_stack(monkeypatch)
    monkeypatch.setattr(qwen, "own_vram_mib", lambda: 4648)
    provider = qwen.QwenAsrProvider(Path("model"), "cuda")
    monkeypatch.setattr(provider, "recognise", lambda samples: None)
    assert provider.load() == {"device": "cuda", "vram_mib": 4648}
    assert loaded["device_map"] == "cuda" and loaded["local_files_only"] is True


def test_load_does_not_echo_the_option_when_the_weights_are_elsewhere(monkeypatch):
    from levi_stt_worker import qwen
    from levi_stt_worker.errors import StartupError

    stub_stack(monkeypatch, devices=("cpu",))
    provider = qwen.QwenAsrProvider(Path("model"), "cuda")
    monkeypatch.setattr(provider, "recognise", lambda samples: pytest.fail("warmed up"))
    with pytest.raises(StartupError):
        provider.load()


@pytest.mark.parametrize("failing", ["warm-up", "vram probe"])
def test_load_turns_a_failure_after_the_model_load_into_a_startup_error(
    monkeypatch, capsys, failing
):
    """An out-of-memory error or kernel failure in the warm-up (or an odd
    nvidia-smi) must reach the service as a startup error, not a traceback."""
    from levi_stt_worker import qwen
    from levi_stt_worker.common import EXIT_STARTUP
    from levi_stt_worker.errors import StartupError

    secret = "CUDA out of memory /home/marvel/work/x transcript"
    stub_stack(monkeypatch)
    provider = qwen.QwenAsrProvider(Path("model"), "cuda")

    def boom(*_):
        raise RuntimeError(secret)

    if failing == "warm-up":
        monkeypatch.setattr(provider, "recognise", boom)
    else:
        monkeypatch.setattr(provider, "recognise", lambda samples: None)
        monkeypatch.setattr(qwen, "own_vram_mib", boom)
    with pytest.raises(StartupError) as caught:
        provider.load()
    assert caught.value.code == "internal" and caught.value.exit_code == EXIT_STARTUP
    assert "RuntimeError" in caught.value.message and secret not in caught.value.message
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert secret not in capsys.readouterr().err


def test_a_startup_error_from_the_load_is_not_rewrapped(monkeypatch):
    from levi_stt_worker import qwen
    from levi_stt_worker.errors import StartupError

    stub_stack(monkeypatch, cuda=False)
    with pytest.raises(StartupError) as caught:
        qwen.QwenAsrProvider(Path("model"), "cuda").load()
    assert "CUDA is not available" in caught.value.message


# --------------------------------------------------------------- serve loop


class ScriptedProvider:
    name = "test"

    def __init__(self, behaviours):
        self.behaviours = list(behaviours)
        self.seen: list[tuple[str, float]] = []

    def load(self):
        return {}

    def transcribe(self, request_id, clip):
        self.seen.append((request_id, clip.seconds))
        action = self.behaviours.pop(0)
        if isinstance(action, Exception):
            raise action
        return action

    def close(self):
        pass


HEARD = {
    "heard": True,
    "language": "English",
    "text": "pick the eggplant",
    "avg_logprob": -0.1,
    "speech_s": 1.0,
}


def request(request_id: str, payload: bytes, **header) -> bytes:
    head = {"op": "transcribe", "id": request_id, "bytes": len(payload), **header}
    return json.dumps(head).encode() + b"\n" + payload


def run_loop(stream: bytes, provider, max_seconds: float = 10.0):
    from levi_stt_worker.serve import Emitter, serve_loop

    reader, writer = io.BytesIO(stream), io.BytesIO()
    code = serve_loop(reader, Emitter(writer), provider, max_seconds)
    events = [json.loads(line) for line in writer.getvalue().splitlines()]
    return code, events, reader


def test_loop_answers_each_request_in_order_and_exits_on_shutdown():
    provider = ScriptedProvider([HEARD, {"heard": False, "reason": "no_speech"}])
    stream = (
        request("r1", wav_bytes(1.0))
        + request("r-2", wav_bytes(2.0))
        + b'{"op":"shutdown"}\n'
    )
    code, events, reader = run_loop(stream + request("never", wav_bytes(1.0)), provider)
    assert code == 0
    assert events == [
        {"event": "result", "id": "r1", **HEARD},
        {"event": "result", "id": "r-2", "heard": False, "reason": "no_speech"},
    ]
    assert provider.seen == [("r1", pytest.approx(1.0)), ("r-2", pytest.approx(2.0))]
    assert b"never" in reader.read()  # the request after shutdown stays unread


def test_loop_stops_reading_at_shutdown():
    provider = ScriptedProvider([])
    tail = request("never", wav_bytes(1.0))
    code, events, reader = run_loop(b'{"op":"shutdown"}\n' + tail, provider)
    assert (code, events, provider.seen) == (0, [], [])
    assert reader.read() == tail  # nothing after shutdown was consumed


def test_loop_clean_eof_exits_zero():
    code, events, _ = run_loop(b"", ScriptedProvider([]))
    assert (code, events) == (0, [])


def test_loop_bad_audio_is_answered_and_the_next_request_still_works():
    provider = ScriptedProvider([HEARD])
    stream = (
        request("bad1", wav_bytes(1.0, rate=8000))
        + request("bad2", b"not a wav at all, but long enough to read " * 3)
        + request("long", wav_bytes(11.0))
        + request("good", wav_bytes(1.0))
    )
    code, events, _ = run_loop(stream, provider)
    assert code == 0
    assert [(e["event"], e["id"], e.get("code")) for e in events] == [
        ("error", "bad1", "audio_invalid"),
        ("error", "bad2", "audio_invalid"),
        ("error", "long", "too_long"),
        ("result", "good", None),
    ]
    assert [seen[0] for seen in provider.seen] == [
        "good"
    ]  # the provider never saw the bad clips


def test_loop_zero_byte_request_is_an_audio_error_not_a_desync():
    provider = ScriptedProvider([HEARD])
    code, events, _ = run_loop(
        request("empty", b"") + request("good", wav_bytes(1.0)), provider
    )
    assert code == 0
    assert [e.get("code") or e["event"] for e in events] == ["audio_invalid", "result"]


def test_loop_a_provider_failure_is_internal_without_its_message_and_does_not_end_the_worker(
    capsys,
):
    secret = "test-secret"
    provider = ScriptedProvider([RuntimeError(secret), HEARD])
    code, events, _ = run_loop(
        request("a", wav_bytes(1.0)) + request("b", wav_bytes(1.0)), provider
    )
    assert code == 0
    assert (
        events[0]["event"] == "error"
        and events[0]["code"] == "internal"
        and events[0]["id"] == "a"
    )
    assert secret not in json.dumps(events) and "RuntimeError" in events[0]["message"]
    assert events[1]["event"] == "result"
    assert secret not in capsys.readouterr().err


def test_loop_a_scripted_request_error_keeps_its_code():
    from levi_stt_worker.errors import RequestError

    provider = ScriptedProvider([RequestError("too_long", "scripted")])
    _, events, _ = run_loop(request("a", wav_bytes(1.0)), provider)
    assert events == [
        {"event": "error", "code": "too_long", "message": "scripted", "id": "a"}
    ]


@pytest.mark.parametrize(
    "header",
    [
        b"this is not json\n",
        b"[1, 2]\n",
        b'"transcribe"\n',
        b"\n",
        b'{"op":"transcribe"}\n',
        b'{"op":"transcribe","bytes":10}\n',
        b'{"op":"transcribe","id":"x"}\n',
        b'{"op":"unknown","id":"x","bytes":10}\n',
        b'{"op":"transcribe","id":"x","bytes":10,"extra":1}\n',
        b'{"op":"shutdown","id":"x"}\n',
        b'{"op":"transcribe","id":"x","bytes":-1}\n',
        b'{"op":"transcribe","id":"x","bytes":1.5}\n',
        b'{"op":"transcribe","id":"x","bytes":"10"}\n',
        b'{"op":"transcribe","id":"x","bytes":true}\n',
        b'{"op":"transcribe","id":"","bytes":10}\n',
        b'{"op":"transcribe","id":"a b","bytes":10}\n',
        b'{"op":"transcribe","id":"a\\nb","bytes":10}\n',
        b'{"op":"transcribe","id":42,"bytes":10}\n',
        b'{"op":"transcribe","id":"' + b"x" * 129 + b'","bytes":10}\n',
        b'\xff\xfe{"op":"shutdown"}\n',
        b"{" + b" " * 2000 + b"}\n",  # longer than the header limit
        b'{"op":"shutdown"}',  # no newline at EOF: incomplete
    ],
)
def test_loop_a_bad_header_is_answered_once_and_the_worker_stops_without_guessing(
    header,
):
    from levi_stt_worker.common import EXIT_PROTOCOL

    provider = ScriptedProvider([HEARD])
    follow_up = request("valid-follow-up", wav_bytes(1.0))
    code, events, _ = run_loop(header + follow_up, provider)
    assert code == EXIT_PROTOCOL
    assert (
        len(events) == 1
        and events[0]["event"] == "error"
        and events[0]["code"] == "internal"
    )
    assert provider.seen == []  # the request that follows is never taken for a request


@pytest.mark.parametrize(
    "stream", [b'{"op":"shutdown"}', b'{"op":"transcribe","id":"x","by']
)
def test_loop_a_header_cut_off_by_eof_is_an_error_not_a_shutdown(stream):
    from levi_stt_worker.common import EXIT_PROTOCOL

    code, events, _ = run_loop(stream, ScriptedProvider([]))
    assert code == EXIT_PROTOCOL
    assert [e["event"] for e in events] == ["error"]


def test_loop_a_bad_header_after_good_requests_does_not_borrow_an_old_id():
    provider = ScriptedProvider([HEARD])
    code, events, _ = run_loop(
        request("first", wav_bytes(1.0)) + b"garbage\n", provider
    )
    assert code != 0
    assert events[1]["event"] == "error" and "id" not in events[1]


def test_loop_an_oversized_announcement_is_refused_without_reading_the_payload():
    from levi_stt_worker.common import EXIT_PROTOCOL, MAX_REQUEST_BYTES

    provider = ScriptedProvider([])
    payload = b"\x00" * 1000
    stream = (
        json.dumps(
            {"op": "transcribe", "id": "big", "bytes": MAX_REQUEST_BYTES + 1}
        ).encode()
        + b"\n"
        + payload
    )
    code, events, reader = run_loop(stream, provider)
    assert code == EXIT_PROTOCOL
    assert [e["code"] for e in events] == ["too_long"]
    assert reader.read() == payload  # still unread: nothing is drained or guessed
    # the limit itself is accepted as a size (the WAV check then judges it)
    stream = request("max", b"\x00" * MAX_REQUEST_BYTES)
    code, events, _ = run_loop(stream, ScriptedProvider([]))
    assert code == 0 and events[0]["code"] == "audio_invalid"


def test_loop_a_short_read_ends_the_worker():
    from levi_stt_worker.common import EXIT_PROTOCOL

    provider = ScriptedProvider([HEARD])
    full = wav_bytes(1.0)
    head = (
        json.dumps({"op": "transcribe", "id": "cut", "bytes": len(full)}).encode()
        + b"\n"
    )
    code, events, _ = run_loop(head + full[: len(full) // 2], provider)
    assert code == EXIT_PROTOCOL
    assert [(e["code"], e["id"]) for e in events] == [("audio_invalid", "cut")]
    assert provider.seen == []


def test_loop_logs_hash_and_size_but_never_the_transcript(capsys):
    text = "pick the unmistakable-eggplant"
    provider = ScriptedProvider([{**HEARD, "text": text}])
    payload = wav_bytes(1.0)
    run_loop(request("log-id", payload), provider)
    err = capsys.readouterr().err
    assert "log-id" in err and f"bytes={len(payload)}" in err and "sha256=" in err
    assert text not in err and "unmistakable" not in err


def test_emitter_output_is_ascii_json_lines():
    from levi_stt_worker.serve import Emitter

    out = io.BytesIO()
    Emitter(out)({"event": "result", "text": "把茄子   放到"})
    raw = out.getvalue()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    raw.decode("ascii")
    assert json.loads(raw)["text"] == "把茄子   放到"


# ----------------------------------------------------------------- process


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("8-15", set(range(8, 16))), ("3", {3}), ("0,2,4-5", {0, 2, 4, 5}), ("6-6", {6})],
)
def test_parse_cpus(spec, expected):
    from levi_stt_worker.serve import parse_cpus

    assert parse_cpus(spec) == expected


@pytest.mark.parametrize("spec", ["", "a-b", "5-2", "-3", "1,,2", "1-"])
def test_parse_cpus_rejects_malformed_specs(spec):
    from levi_stt_worker.serve import parse_cpus

    with pytest.raises(ValueError):
        parse_cpus(spec)


# ---------------------------------------------------------------- installer


def snapshot(root: Path) -> Path:
    source = root / "snapshot"
    (source / ".cache" / "huggingface").mkdir(parents=True)
    (source / ".cache" / "huggingface" / "meta").write_text("x")
    (source / "config.json").write_text("{}")
    (source / "model.safetensors").write_bytes(b"weights")
    return source


def test_install_from_a_snapshot_writes_a_verified_manifest_and_skips_hidden_files(
    tmp_path, monkeypatch
):
    import hashlib

    from levi_stt_worker import install
    from levi_stt_worker.manifest import verify_manifest

    monkeypatch.setitem(
        install.MODELS,
        "tiny",
        ("org/tiny", "rev", hashlib.sha256(b"weights").hexdigest()),
    )
    models = tmp_path / "models"
    source = snapshot(tmp_path)
    target = install.install_model(models, "tiny", source)
    assert target == models / "tiny"
    assert sorted(verify_manifest(target)) == ["config.json", "model.safetensors"]
    assert sorted(path.name for path in models.iterdir()) == [
        "tiny"
    ]  # no .partial or scratch left
    # installing again leaves a verified folder alone
    assert install.install_model(models, "tiny", source) == target


def test_install_refuses_weights_that_are_not_the_pinned_revision_and_leaves_nothing(
    tmp_path, monkeypatch
):
    from levi_stt_worker import install
    from levi_stt_worker.errors import StartupError

    monkeypatch.setitem(install.MODELS, "tiny", ("org/tiny", "rev", "0" * 64))
    models = tmp_path / "models"
    with pytest.raises(StartupError) as caught:
        install.install_model(models, "tiny", snapshot(tmp_path))
    assert caught.value.code == "manifest_mismatch"
    assert list(models.iterdir()) == []


def test_install_does_not_overwrite_a_folder_that_fails_verification(
    tmp_path, monkeypatch
):
    from levi_stt_worker import install
    from levi_stt_worker.errors import StartupError

    monkeypatch.setitem(install.MODELS, "tiny", ("org/tiny", "rev", "0" * 64))
    existing = tmp_path / "models" / "tiny"
    existing.mkdir(parents=True)
    (existing / "keep.me").write_text("user file")
    with pytest.raises(StartupError):
        install.install_model(tmp_path / "models", "tiny", snapshot(tmp_path))
    assert (existing / "keep.me").read_text() == "user file"


def test_install_knows_only_pinned_models():
    from levi_stt_worker import install
    from levi_stt_worker.common import MODELS

    with pytest.raises(ValueError):
        install.install_model(Path("/nonexistent"), "some/other-model")
    for repo, revision, weights_sha in MODELS.values():
        assert repo.startswith("Qwen/Qwen3-ASR-") and repo.endswith("-hf")
        assert len(revision) == 40 and len(weights_sha) == 64
