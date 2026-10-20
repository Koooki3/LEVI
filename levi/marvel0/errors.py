"""One error shape for the marvel0 routes that read a site file or run a command
preview or a policy deploy (``catalog_api.py``, ``deploy_api.py``): an
``HTTPException`` whose ``detail`` is ``{"code", "message", ...extra}``, the
shape of ``levi.automatic.api._fail``. FastAPI's own 422 for a malformed body
keeps FastAPI's default shape.
"""

from fastapi import HTTPException

SITE_MISSING = "site_missing"
SITE_INVALID = "site_invalid"


def fail(status: int, code: str, message: str, **extra) -> HTTPException:
    """The refusal ``code`` (a stable name a page can translate) with an
    English ``message`` and any further facts as ``extra``."""
    return HTTPException(status, {"code": code, "message": message, **extra})
