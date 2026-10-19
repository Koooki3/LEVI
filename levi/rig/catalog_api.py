"""The rig console's model list, site view and client command preview:

- ``GET /api/levi/rig/models``: the models the policy deploy panel may deploy
  (``models.py``), with the reasons for the ones it left out;
- ``GET /api/levi/rig/site``: the site file as the page may see it (no path);
- ``POST /api/levi/rig/command/preview``: the demo client command for a form,
  as text to copy (``command.py``). **Nothing is executed, started, connected
  to or written**; a person pastes the command into a terminal.

The local authentication is the service's own (the web UI token; a Bearer
credential is refused by its middleware), as for ``GET /api/levi/rig/status``.
Every route first needs the site file (``site.require()``): without it, or with
an invalid one, the answer is 412 and nothing else has been read. Errors have
the shape of ``errors.fail``; FastAPI's own 422 answers a malformed body. No
answer names a path on the machine except the preview's text to copy (``cwd``,
``argv``, ``command``) and the missing site file's own location.
"""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from . import command, models
from . import site as rig_site
from .errors import fail

router = APIRouter(prefix="/api/levi/rig", tags=["Rig"])
SCHEMA = "levi.rig.command.v1"


class PreviewBody(BaseModel):
    """The form. Strict (``"5"`` is not 5, ``1`` is not true) and closed: a
    field outside the form, such as ``levi_mode``, is refused."""

    model_config = ConfigDict(extra="forbid", strict=True)

    model_id: str
    prompt: str
    max_steps: int | None = None
    eval_num: int | None = None
    min_z: float | None = None
    rtc: bool | None = None
    rtc_horizon: int | None = None
    rollout_group: str | None = None
    note: str | None = None


@router.get("/models")
def models_view():
    """The checkpoints that may be deployed, each with its serving config, the
    ``unverified`` label where no real-GPU check is recorded, and the default
    rollout group; ``skipped`` says why the others are not offered."""
    site = rig_site.require()
    return models.view(models.current(site), site)


@router.get("/site")
def site_view():
    """The site file without its paths: connection, camera serials, home pose,
    limits, caps, fixed client flags, GPU fractions and unit settings."""
    return rig_site.public_view(rig_site.require())


@router.post("/command/preview")
def command_preview(body: PreviewBody):
    """The client command for this form, validated and rendered as text to
    copy. Executes nothing and writes nothing."""
    site = rig_site.require()
    model = models.get(site, body.model_id)
    if model is None:
        raise fail(
            422,
            "model_unknown",
            "this model is not offered",
            field="model_id",
        )
    try:
        form = command.validate(site, model, body.model_dump())
        preview = command.render(site, model, form)
    except command.FormError as exc:
        raise fail(422, exc.code, exc.message, field=exc.field, **exc.extra) from exc
    except command.SelfCheckError as exc:
        raise fail(
            500,
            "command_self_check",
            "the command failed its own check and is not shown",
        ) from exc
    return {
        "schema": SCHEMA,
        "ok": True,
        "model": {
            "id": model.id,
            "config": model.config,
            "restricted_prompts": model.restricted,
            "verified": model.verified,
        },
        # The policy server this client needs: an RTC client needs the RTC one.
        "server_requirements": {
            "model_id": model.id,
            "config": model.config,
            "rtc": form.rtc,
        },
        "cwd": preview.cwd,
        "env": preview.env,
        "argv": preview.argv,
        "command": preview.command,
        "warnings": preview.warnings,
        "hands": command.HANDS,
    }
