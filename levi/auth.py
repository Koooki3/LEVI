"""Request-scoped Hub credentials; never written to job or report files."""

import os
from contextvars import ContextVar

hub_token = ContextVar("levi_hub_token", default=None)


def token():
    return hub_token.get() or os.getenv("HF_TOKEN") or None
