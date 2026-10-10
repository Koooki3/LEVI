"""The live pipeline's link to the labelling engine it shares with the
product: which of ``runs.plan``'s workflow options it sets, which new ones it
does not know about yet, and what each automatic result records about how it
was made.

The background worker plans its runs through the same ``runs.plan`` a person's
subtask annotation uses, with a ``workflow`` dictionary the worker builds
(``worker.py``, ``context``). Three things follow from that, all handled here:

* ``[pipeline.workflow]`` in ``live.toml`` merges extra workflow options into
  the time-segment (temporal) workflow, checked by the very model ``runs.plan``
  checks (``levi.agent.planning.Workflow``): a new option such as
  ``event_intelligence = { mode = "candidates" }`` needs no code change.
* ``drift`` lists the options this LEVI accepts that the live pipeline neither
  sets nor has knowingly left at their default, so an upgrade that adds an
  option is noticed (``levi live doctor``). It only reports.
* ``stamp`` / ``combine`` make the ``capability`` record kept as a top-level
  key of every episode's record in the dataset state (and of its statistics
  row, at most ``MAX_BYTES``): LEVI commit and version, plan digests, spec,
  event-intelligence mode and algorithm hash, time. Readers must tolerate its
  absence (older results have none): ``of`` does.

Importing this module loads nothing heavy; ``levi.agent.planning`` (pydantic)
is imported only inside the functions that need the workflow model.
"""

import contextlib
import functools
import subprocess
import time
from pathlib import Path

SCHEMA = "levi.live.capability.v1"

# Workflow options the worker sets itself, from other settings of the live
# pipeline: ``[pipeline.workflow]`` may not name them (change the setting).
OWNED = {
    "kind": "fixed by the stage (temporal or review)",
    "definitions": "the generic subtask vocabulary (pipeline.vocabulary)",
    "coarse_step_seconds": "pipeline.coarse_step_seconds",
    "max_evidence_frames": "chosen from the model's image limit",
    "refine": "pipeline.refine",
    "pilot_episode": "the batch's first episode",
    "require_human_pilot": "false for the live pipeline (the plan waives it)",
    "anchored": "pipeline.anchored_spec",
}
# Options of the workflow the live pipeline has no use for and so leaves at
# their defaults on purpose; a new option is not in this list, which is how
# ``drift`` finds it.
ACKNOWLEDGED = {
    "overlapping_layers": "one layer of time segments",
    "boundary_window_seconds": "the refinement window follows the coarse step",
    "boundary_tolerance_seconds": "the planner's default",
    "object_concepts": "object masks are not part of the live pipeline",
    "mask_definition": "object masks are not part of the live pipeline",
    "final_samples": "the release review chooses its frames from the anchor",
}


def workflow_model():
    """The model ``runs.plan`` checks a workflow against."""
    from levi.agent.planning import Workflow

    return Workflow


def options() -> dict:
    """``{option: default}`` for every option this LEVI's workflow accepts."""
    out = {}
    for name, field in workflow_model().model_fields.items():
        default = field.get_default(call_default_factory=True)
        if hasattr(default, "model_dump"):
            default = default.model_dump()
        out[name] = default
    return out


def problems(config) -> list:
    """Why ``[pipeline.workflow]`` cannot be used (empty list: it can). The
    keys are checked by ``runs.plan``'s own workflow model, as part of a
    temporal workflow, so an unknown or invalid key is refused here, at start,
    with the model's own words."""
    extra = config.pipeline.workflow
    if not extra:
        return []
    if not isinstance(extra, dict) or not all(isinstance(k, str) for k in extra):
        return ["pipeline.workflow must be a table of workflow options"]
    out = [
        f"pipeline.workflow.{key} is set by the live pipeline itself ({why}); "
        "change that setting instead"
        for key, why in OWNED.items()
        if key in extra
    ]
    if out:
        return out
    from pydantic import ValidationError

    try:
        workflow_model().model_validate({"kind": "temporal", **extra})
    except ValidationError as exc:
        for error in exc.errors():
            where = ".".join(str(p) for p in error["loc"])
            out.append(f"pipeline.workflow.{where}: {error['msg']}")
    return out or []


def drift(config) -> list:
    """Options this LEVI's workflow accepts that the live pipeline neither sets
    (itself or through ``[pipeline.workflow]``) nor has declared it leaves at
    the default: ``[{"option", "default"}]``, nothing changed."""
    known = set(OWNED) | set(ACKNOWLEDGED) | set(config.pipeline.workflow)
    return [
        {"option": name, "default": default}
        for name, default in options().items()
        if name not in known
    ]


# --- what an automatic result records ------------------------------------------


@functools.lru_cache(maxsize=1)
def levi_version() -> dict:
    """``{"commit", "version"}`` of the running LEVI: the checkout's git head
    and the installed version; either is None when it cannot be told."""
    checkout = Path(__file__).resolve().parents[2]
    commit = version = None
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        done = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        text = done.stdout.strip()
        commit = text if len(text) == 40 else None
    with contextlib.suppress(Exception):  # a record, not a requirement
        from importlib import metadata

        version = metadata.version("levi")
    return {"commit": commit, "version": version}


def stamp(run, *, kind, spec=None, at=None) -> dict:
    """The ``capability`` record for a result made by ``run`` (a run dict as
    ``runs.get`` returns it; absent fields read as None). ``spec`` is the
    review's ``(id, version)``."""
    run = run or {}
    flow = (run.get("context") or {}).get("workflow") or {}
    events = flow.get("event_intelligence") or {}
    algorithm = run.get("event_algorithm") or {}
    return {
        "schema": SCHEMA,
        "kind": kind,
        "levi": dict(levi_version()),
        "plan_digest": (run.get("plan") or {}).get("digest"),
        "spec": f"{spec[0]}@{spec[1]}" if spec and spec[0] else None,
        "event_intelligence": {
            "mode": events.get("mode") or "off",
            "algorithm_sha256": algorithm.get("sha256") if events else None,
        },
        "at": time.time() if at is None else at,
    }


MAX_BYTES = 4096


def combine(old, new) -> dict:
    """The episode's ``capability`` (top-level key of its record in the
    dataset state and of its statistics row) after a stage that made ``new``
    (a ``stamp``). The time segments and the release review are two runs of
    one episode: ``plans`` keeps each stage's plan digest, ``plan_digest`` is
    the latest, ``spec`` the review's, ``event_intelligence`` the time-segment
    run's (the review has none). Always well under ``MAX_BYTES``."""
    base = dict(old) if isinstance(old, dict) and old.get("schema") == SCHEMA else {}
    plans = dict(base.get("plans") or {})
    plans[new["kind"]] = new["plan_digest"]
    out = {
        "schema": SCHEMA,
        "levi": new["levi"],
        "plan_digest": new["plan_digest"],
        "plans": plans,
        "spec": new["spec"] or base.get("spec"),
        "event_intelligence": new["event_intelligence"]
        if new["kind"] == "temporal" or "event_intelligence" not in base
        else base["event_intelligence"],
        "at": new["at"],
    }
    return out


def of(part) -> dict | None:
    """The ``capability`` record of an episode's record (or any dict that
    carries one), or None for one made before the record existed or damaged."""
    value = part.get("capability") if isinstance(part, dict) else None
    return value if isinstance(value, dict) and value.get("schema") == SCHEMA else None
