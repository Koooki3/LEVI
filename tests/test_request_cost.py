"""A model's cost per character and per image, learned from its requests."""

import pytest

from levi.agent.schema import ProviderConfig
from levi.inference import request_cost


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(request_cost, "_state_dir", lambda: tmp_path)


def config():
    return ProviderConfig(
        name="qwen-local",
        kind="ollama",
        base_url="http://127.0.0.1:11435",
        model="qwen3.5:4b",
        model_digest="a" * 64,
        allow_localhost=True,
    )


def test_the_fit_splits_text_from_images():
    assert request_cost.fitted(config()) is None
    # 0.3 tokens a character, 300 an image.
    for chars, images in ((16000, 7), (20000, 36), (9000, 0)):
        request_cost.record(config(), chars, images, int(0.3 * chars + 300 * images))
    per_char, per_image = request_cost.fitted(config())
    assert per_char == pytest.approx(0.3 * request_cost.MARGIN, rel=0.01)
    assert per_image == pytest.approx(300 * request_cost.MARGIN, rel=0.01)


def test_samples_that_cannot_tell_the_two_apart_give_no_fit():
    for _ in range(3):
        request_cost.record(config(), 1000, 5, 2000)
    assert request_cost.fitted(config()) is None


def test_calibration_measures_an_image_with_two_one_token_calls(monkeypatch, tmp_path):
    from levi.inference import gpu, provider

    sent = []

    class Client:
        def chat(self, model, digest, messages, **options):
            sent.append(options["max_output_tokens"])
            message = messages[0]
            tokens = len(message["content"]) // 4 + 300 * len(message.get("images", []))
            return {"usage": {"tokens": tokens + 1, "prompt_tokens": tokens}}

    monkeypatch.setattr(provider, "client_for", lambda *a, **k: Client())
    monkeypatch.setattr(gpu, "require_free", lambda c=None: None)
    frames = []
    for i in range(3):
        frames.append(tmp_path / f"f{i}.png")
        frames[-1].write_bytes(b"png")
    fit, spent = request_cost.calibrate(config(), "x" * 4000, frames)
    assert sent == [1, 1] and spent > 600
    assert fit[1] == pytest.approx(300 * request_cost.MARGIN, rel=0.05)


def test_the_model_reads_a_compact_ledger_in_image_order():
    from levi.inference.provider import model_view

    ledger = [
        {"id": "a", "timestamp": 0.0, "artifact": "a.png", "sha256": "f" * 64},
        {"id": "note", "text": "state"},
        {"id": "b", "timestamp": 0.4, "artifact": "b.png", "crop_xyxy": [0, 0, 5, 5]},
    ]
    assert model_view(ledger) == [
        {"id": "a", "image": 1, "timestamp": 0.0},
        {"id": "note", "text": "state"},
        {"id": "b", "image": 2, "timestamp": 0.4, "crop_xyxy": [0, 0, 5, 5]},
    ]
