"""The camera preview of the marvel0 page (``preview.py``):

- ``GET /api/levi/marvel0/preview``: the ``levi.marvel0.preview.v1`` document:
  ``off``, ``starting``, ``on``, ``stopping``, ``refused`` (it cannot start now,
  with the reason) or ``failed`` (the worker could not open the cameras, with
  the reason), the age of the newest picture of each camera, what is left of
  the idle and lifetime limits and whether the last stop needed SIGKILL
  (``killed``);
- ``POST /api/levi/marvel0/preview/start``: open the two cameras for a preview
  (refused with a reason code while a client may hold them, and in any LEVI that
  is not the product: ``not_product``);
- ``POST /api/levi/marvel0/preview/stop``: let the cameras go (idempotent);
- ``GET /api/levi/marvel0/preview/frame?camera=side|wrist``: the newest picture
  as a JPEG; a request counts as "the page is still looking".

The two writes are **a person's**: the same local authentication as the policy
deploy, and any Bearer credential is refused (``deploy_api.person_only``). The
preview moves nothing and is undone by Stop; it stops by itself 20 s after the
last picture was asked for and 10 minutes after it started, and every start of
a client stops it first.

Errors have the shape of ``errors.fail``. No answer names a path or a serial.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Response

from . import preview
from .deploy_api import person_only
from .errors import fail

router = APIRouter(prefix="/api/levi/marvel0", tags=["marvel0"])


@router.get("/preview")
def preview_view():
    """The preview's state and whether it may start now."""
    return preview.manager().view()


@router.post("/preview/start", status_code=202, dependencies=[Depends(person_only)])
def preview_start():
    """Open the cameras for a preview (person only)."""
    try:
        return preview.manager().start()
    except preview.Refused as exc:
        raise fail(exc.status, exc.code, exc.message, **exc.extra) from exc


@router.post("/preview/stop", dependencies=[Depends(person_only)])
def preview_stop():
    """Let the cameras go and wait (up to 6 s) until the worker has exited."""
    manager = preview.manager()
    manager.stop("requested")
    return manager.view()


@router.get("/preview/frame")
def preview_frame(camera: Literal["side", "wrist"]):
    """The newest picture of one camera (``image/jpeg``). 404 with a reason
    code (``preview_off``, ``no_frame``) when there is none; a camera other
    than ``side`` or ``wrist`` is a 422."""
    try:
        found = preview.manager().frame(camera)
    except preview.NoFrame as exc:
        raise fail(404, exc.reason, exc.message) from exc
    return Response(
        content=found.jpeg,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Levi-Frame-Age-Ms": str(round(found.age_s * 1000)),
        },
    )
