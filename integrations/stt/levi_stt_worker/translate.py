"""Object-name translation for the voice card's generated sentence.

The service asks for it when a spoken task names an object in Chinese that the
matcher's lexicon does not have (``levi/marvel0/speech_template.py``). For each
name the worker gives three model answers and nothing else:

- ``english``: the name in English, as short robot instructions write it;
- ``chinese``: that English translated back into Chinese;
- ``same``: whether the model thinks the Chinese and the English name the same
  kind of object.

Deciding whether a translation may be used is the service's job
(``levi/marvel0/speech_translate.py``, standard library, tested without a GPU);
the worker never judges and never logs a name. The text model is loaded on the
first request, not at start, so a worker that only transcribes holds only the
speech model. Greedy decoding, at most ``TRANSLATE_MAX_NEW_TOKENS`` new tokens per
answer, one short JSON object per answer.

Prompts: the ones measured in the 2026-10-31 evaluation (levi-hub
programs/rig-console/reviews/voice-translate-20261031.md). Changing a prompt
changes what that evaluation measured: re-run it.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .common import TRANSLATE_ANSWER_CHARS, TRANSLATE_MAX_NEW_TOKENS, clean_text, log
from .errors import RequestError, StartupError

SYSTEM_FORWARD = (
    "You translate the Chinese name of a physical object, one that a robot arm can pick up or put "
    "something on, into the English name used in short robot instructions such as "
    '"put the eggplant into the blue bowl". Answer only with JSON {"english": "..."}. '
    "The English name is lowercase, singular unless the object is a pair (scissors, glasses), "
    "one to three words, with no article and no explanation. If the Chinese text is not the name of "
    "an object (an action, a direction, a colour, a number, a command such as stop, a pronoun), "
    'answer {"english": "NONE"}.'
)
SHOTS_FORWARD = (
    ("茄子", "eggplant"),
    ("毛绒玩具", "plush toy"),
    ("易拉罐", "soda can"),
    ("停止", "NONE"),
    ("往右", "NONE"),
)
SYSTEM_BACK = (
    "You translate the English name of a physical object into the most common Chinese name for it. "
    'Answer only with JSON {"chinese": "..."}.'
)
SHOTS_BACK = (("eggplant", "茄子"), ("plush toy", "毛绒玩具"), ("soda can", "易拉罐"))
SYSTEM_SAME = (
    "Decide whether a Chinese object name and an English object name name the same kind of object. "
    'Answer only with JSON {"same": true} or {"same": false}.'
)
SHOTS_SAME = (
    (("茄子", "eggplant"), True),
    (("橙子", "lemon"), False),
    (("杯子", "cup"), True),
    (("勺子", "fork"), False),
)


def _messages(system: str, shots, query: str) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": system}]
    for question, answer in shots:
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": answer})
    messages.append({"role": "user", "content": query})
    return messages


def forward_messages(name: str) -> list[dict[str, str]]:
    shots = [
        (q, json.dumps({"english": a}, ensure_ascii=False)) for q, a in SHOTS_FORWARD
    ]
    return _messages(SYSTEM_FORWARD, shots, name)


def back_messages(english: str) -> list[dict[str, str]]:
    shots = [(q, json.dumps({"chinese": a}, ensure_ascii=False)) for q, a in SHOTS_BACK]
    return _messages(SYSTEM_BACK, shots, english)


def same_messages(name: str, english: str) -> list[dict[str, str]]:
    shots = [
        (f"Chinese: {zh}\nEnglish: {en}", json.dumps({"same": same}))
        for (zh, en), same in SHOTS_SAME
    ]
    return _messages(SYSTEM_SAME, shots, f"Chinese: {name}\nEnglish: {english}")


def field(text: str, key: str) -> str | None:
    """The string value of ``key`` in a model answer (JSON, or JSON-like text)."""
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]*)"', text or "")
    return clean_text(match.group(1))[:TRANSLATE_ANSWER_CHARS] if match else None


def same_field(text: str) -> bool | None:
    match = re.search(r'"same"\s*:\s*(true|false)', text or "")
    return None if match is None else match.group(1) == "true"


def _free_gpu() -> None:
    """Give back what a failed load allocated (best effort, no import of its
    own: nothing was allocated if torch was never imported)."""
    import contextlib
    import sys

    torch = sys.modules.get("torch")
    if torch is not None:
        with contextlib.suppress(Exception):  # nothing more to free
            torch.cuda.empty_cache()


class QwenTextTranslator:
    """A transformers-native chat model (bf16, entirely on the GPU) that is
    loaded on the first ``translate`` call and kept until the worker exits."""

    def __init__(self, folder: Path, device: str) -> None:
        self._folder = folder
        self._device = device
        self._model: Any = None
        self._tokenizer: Any = None
        # Why the model cannot be loaded, once found: asked again it answers at
        # once instead of hashing 7.6 GB and failing the same way.
        self._broken: str | None = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> int:
        """Load the model, or raise ``RequestError("translate_unavailable")``
        (remembered) for a folder that fails its manifest, cannot be read or
        does not fit entirely on the GPU, so the speech worker stays. A CUDA
        error or out-of-memory propagates: the service then replaces the
        worker, which frees what was allocated."""
        if self._broken:
            raise RequestError("translate_unavailable", self._broken)
        began = time.perf_counter()
        try:
            from .manifest import verify_manifest

            verify_manifest(self._folder)
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(
                self._folder, local_files_only=True
            )
            model = AutoModelForCausalLM.from_pretrained(
                self._folder,
                dtype=torch.bfloat16,
                device_map=self._device,
                attn_implementation="sdpa",
                local_files_only=True,
                use_safetensors=True,
            ).eval()
        except (StartupError, ImportError, OSError, ValueError) as error:
            message = (
                f"The translation model cannot be loaded ({type(error).__name__})."
            )
            if not isinstance(error, OSError):  # a read error may pass
                self._broken = message
            _free_gpu()
            raise RequestError("translate_unavailable", message) from None
        if {p.device.type for p in model.parameters()} != {"cuda"}:
            # An offloaded layer would run a 4B model on the arm controller's cores.
            del model
            _free_gpu()
            self._broken = "The translation model is not fully on the GPU."
            raise RequestError("translate_unavailable", self._broken)
        self._tokenizer, self._model = tokenizer, model
        return round((time.perf_counter() - began) * 1000)

    def _answer(self, messages: list[dict[str, str]]) -> str:
        import torch

        prompt = self._tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
        )
        batch = self._tokenizer(prompt, return_tensors="pt").to(self._model.device)
        with torch.inference_mode():
            out = self._model.generate(
                **batch,
                max_new_tokens=TRANSLATE_MAX_NEW_TOKENS,
                do_sample=False,
                pad_token_id=self._tokenizer.eos_token_id,
            )
        return self._tokenizer.decode(
            out[0, batch["input_ids"].shape[1] :], skip_special_tokens=True
        )

    def translate(self, names: list[str]) -> dict[str, Any]:
        load_ms, loaded_now = 0, False
        if self._model is None:
            load_ms, loaded_now = self._load(), True
        began = time.perf_counter()
        items = []
        for name in names:
            english = field(self._answer(forward_messages(name)), "english")
            chinese, same = None, None
            if english and english.upper() != "NONE":
                chinese = field(self._answer(back_messages(english)), "chinese")
                same = same_field(self._answer(same_messages(name, english)))
            items.append(
                {"name": name, "english": english, "chinese": chinese, "same": same}
            )
        infer_ms = round((time.perf_counter() - began) * 1000)
        log(f"translate names={len(names)} loaded_now={loaded_now} ms={infer_ms}")
        return {
            "items": items,
            "loaded_now": loaded_now,
            "timing_ms": {"load": load_ms, "infer": infer_ms},
        }

    def close(self) -> None:
        self._model = None
        self._tokenizer = None
