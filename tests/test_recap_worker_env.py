"""Checks that run inside the RECAP value worker's own environment
(integrations/recap_value/.venv, or LEVI_RECAP_VALUE_WORKER_PYTHON); skipped
when it is not installed."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
from conftest import with_netguard

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "integrations" / "recap_value"


def worker_python() -> Path | None:
    configured = os.getenv("LEVI_RECAP_VALUE_WORKER_PYTHON")
    candidate = Path(configured) if configured else WORKER / ".venv/bin/python"
    return candidate if candidate.exists() else None


pytestmark = pytest.mark.skipif(
    worker_python() is None, reason="RECAP value worker environment not installed"
)


def test_tokenizer_folder_with_only_a_sentencepiece_model(tmp_path):
    """A Gemma tokenizer folder without tokenizer.json (only tokenizer.model,
    as big_vision / openpi caches ship it) loads as the slow SentencePiece
    tokenizer instead of failing on the fast conversion's protobuf import."""
    script = textwrap.dedent(
        """
        import json, sys
        from pathlib import Path
        import sentencepiece as spm
        from levi_recap_worker.rlinf_provider import tokenizer_for

        work = Path(sys.argv[1])
        folder = work / "tokenizer"
        folder.mkdir()
        corpus = work / "corpus.txt"
        corpus.write_text("Task: stack the plates of same color together.\\n" * 50)
        spm.SentencePieceTrainer.train(
            input=str(corpus), model_prefix=str(folder / "tokenizer"),
            vocab_size=24, model_type="char", pad_id=0, eos_id=1, bos_id=2,
            unk_id=3, minloglevel=2,
        )
        (folder / "tokenizer.vocab").unlink()
        (folder / "tokenizer_config.json").write_text(
            json.dumps({"tokenizer_class": "GemmaTokenizer"})
        )
        tok = tokenizer_for({"base_models": {"tokenizer": "tokenizer"}}, work)
        ids = tok.encode("Task: stack the plates.")
        print(json.dumps({"cls": type(tok).__name__, "ids": ids,
                          "bos": tok.bos_token_id}))
        """
    )
    done = subprocess.run(
        [str(worker_python()), "-c", script, str(tmp_path)],
        cwd=WORKER,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**os.environ, "PYTHONPATH": with_netguard(str(WORKER))},
    )
    assert done.returncode == 0, done.stderr[-2000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result["cls"] == "GemmaTokenizer"
    assert result["ids"][0] == result["bos"] and len(result["ids"]) > 5
