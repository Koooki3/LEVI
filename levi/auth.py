"""Request-scoped Hub credentials; never written to job or report files."""

from __future__ import annotations

import hashlib
import os
from contextvars import ContextVar

hub_token = ContextVar("levi_hub_token", default=None)


def token() -> str | None:
    """Return the current browser token, falling back to HF_TOKEN.

    The raw token is intentionally request-scoped and must never be included in
    persisted plans, job records, logs, or workspace memory.
    """
    return hub_token.get() or os.getenv("HF_TOKEN") or None


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
