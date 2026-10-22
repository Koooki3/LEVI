"""Display-only state under the service's ordinary local authentication."""

from fastapi import APIRouter, Response

from .state import snapshot

router = APIRouter(prefix="/api/levi/robot-studio", tags=["robot-studio"])


@router.get("/state")
def robot_studio_state(response: Response):
    """Read an existing workspace snapshot. No producer or motion is started."""
    response.headers["Cache-Control"] = "no-store"
    return snapshot()
