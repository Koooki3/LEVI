"""The marvel0's model list, site view and client command preview:

- ``GET /api/levi/marvel0/models``: the models the policy deploy panel may deploy
  (``models.py``), with the reasons for the ones it left out;
- ``GET /api/levi/marvel0/site``: the site file as the page may see it (no path),
  and the control-rate options with the numbers behind them;
- ``POST /api/levi/marvel0/command/preview``: the demo client command for a form,
  as text to copy (``command.py``). **Nothing is executed, started, connected
  to or written**; a person pastes the command into a terminal. When the arm
  controller of the stack is the anyspeedup one (the A the page started, else the
  control file's choice), rule v3's ``chunk_accel_alpha`` is that controller's
  preset alpha (an empty field is that alpha, another is refused: 422
  ``chunk_accel_alpha_arm_mismatch``), rules v1 and v2 are refused for a preset
  other than ``today`` (``chunk_accel_rule_arm_preset``), and the default rollout
  group gets a suffix (``arm_facts.py``).

The local authentication is the service's own (the web UI token; a Bearer
credential is refused by its middleware), as for ``GET /api/levi/marvel0/status``.
Every route first needs the site file (``site.require()``): without it, or with
an invalid one, the answer is 412 and nothing else has been read. Errors have
the shape of ``errors.fail``; FastAPI's own 422 answers a malformed body. No
answer names a path on the machine except the preview's text to copy (``cwd``,
``argv``, ``command``) and the missing site file's own location.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from . import arm_facts, command, models
from . import site as marvel0_site
from .errors import fail

router = APIRouter(prefix="/api/levi/marvel0", tags=["marvel0"])
SCHEMA = "levi.marvel0.command.v1"


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
    inference_mode: str | None = None
    vlash_overlap_steps: int | None = None
    chunk_accel: bool | None = None
    chunk_accel_rule: Literal["v1", "v2", "v3"] | None = None
    chunk_accel_lambda: float | None = None  # rule v3 only: --phase-lambda-max
    chunk_accel_alpha: float | None = None  # rule v3 only: --phase-alpha
    rtc_horizon: int | None = None
    rollout_group: str | None = None
    note: str | None = None
    control_hz: int | None = None


@router.get("/models")
def models_view():
    """The checkpoints that may be deployed, each with its serving config, the
    ``unverified`` label where no real-GPU check is recorded, and the default
    rollout group; ``skipped`` says why the others are not offered."""
    site = marvel0_site.require()
    return models.view(models.current(site), site)


@router.get("/site")
def site_view():
    """The site file without its paths: connection, camera serials, home pose,
    limits, caps, fixed client flags, GPU fractions and unit settings; and
    ``control``, the control-rate options and the numbers the page needs to
    show what a rate changes before a command is made."""
    site = marvel0_site.require()
    return {**marvel0_site.public_view(site), "control": command.control_public(site)}


@router.post("/command/preview")
def command_preview(body: PreviewBody):
    """The client command for this form, validated and rendered as text to
    copy. Executes nothing and writes nothing."""
    site = marvel0_site.require()
    model = models.get(site, body.model_id)
    if model is None:
        raise fail(
            422,
            "model_unknown",
            "this model is not offered",
            field="model_id",
        )
    # What the arm controller the stack runs asks of the form: rule v3's alpha is
    # the one of the running A's preset (not the file's: the file can be changed
    # while A runs), the ramp rules and the default group follow it too; with the
    # lab's controller, or no control file, this asks nothing.
    facts = arm_facts.read()
    arm = arm_facts.arm_of(facts)
    try:
        form = command.validate(site, model, body.model_dump(), arm)
        preview = command.render(site, model, form, arm)
        problem = arm_facts.problem_of(facts)
        if problem and form.chunk_accel and form.chunk_accel_rule == "v3":
            preview.warnings.append(command.arm_warning(problem))
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
            "inference_mode": form.inference_mode,
            "vlash_overlap_steps": form.vlash_overlap_steps,
        },
        "cwd": preview.cwd,
        "env": preview.env,
        "argv": preview.argv,
        "command": preview.command,
        "warnings": preview.warnings,
        "control": preview.control,
        "hands": command.HANDS,
    }
