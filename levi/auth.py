"""Request-scoped Hub credentials; never written to job or report files."""

from __future__ import annotations

import hashlib
import os
from contextvars import ContextVar

hub_token = ContextVar("levi_hub_token", default=None)


def token() -> str | None:
    """Return the active browser, environment or local HF CLI token.

    Browser credentials always win for the current request, followed by the
    explicit ``HF_TOKEN`` deployment setting. The final fallback reads the
    Hugging Face CLI cache so ``hf auth login`` works for both checkpoint
    downloads and dataset access. The raw token is never persisted.
    """
    scoped = hub_token.get()
    if scoped:
        return scoped
    configured = os.getenv("HF_TOKEN")
    if configured:
        return configured
    try:
        from huggingface_hub import get_token

        return get_token() or None
    except Exception:  # noqa: BLE001
        return None


def credential_scope(value: str | None = None) -> str:
    """Return a non-reversible cache namespace for the active Hub credential.

    A repository can resolve to different content for different accounts. A
    short digest keeps cached datasets and annotation sidecars from being
    accidentally reused after an account change without exposing credentials.
    """
    raw = value if value is not None else token()
    if not raw:
        return "anonymous"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
