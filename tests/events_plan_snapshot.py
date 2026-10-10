"""What a plan freezes, for plans that do not use event intelligence.

``snapshot()`` computes, for a fixed set of task contexts, everything the
approval contract and the model cache are built from: the normalised context
(``TaskContext.model_dump``), the workflow ``plans.clarify`` returns, the
context part of the model-cache fingerprint and the ``planning.material``
digest of a fixed run. ``fixtures/events/plan-material-before.json`` holds
the values computed on main before ``Workflow.event_intelligence`` existed
(commit cf4600a); ``test_events_planning`` compares the current code with it
byte for byte, so a plan without event intelligence keeps the digest it had.

Regenerate only on purpose (it would hide exactly the drift the test is
for): ``python tests/events_plan_snapshot.py > tests/fixtures/events/plan-material-before.json``.
"""

import json
import sys

GRASP = {
    "id": "grasp",
    "label": "grasp",
    "definition": "close on the plate",
    "starts_when": "fingers touch",
    "ends_when": "object moves",
    "success_when": "object is held",
}

CAMERA = "observation.images.top"

CONTEXTS = {
    "temporal-model": {
        "repo_id": "local/fixture",
        "episodes": [0, 1],
        "instruction": "Annotate the subtasks",
        "provider": "fixture",
        "cameras": [CAMERA],
        "allow_media_egress": True,
        "workflow": {
            "kind": "temporal",
            "definitions": [GRASP],
            "coarse_step_seconds": 0.5,
        },
    },
    "temporal-external": {
        "repo_id": "local/fixture",
        "episodes": [0],
        "instruction": "Annotate the subtasks",
        "provider": "external",
        "cameras": [CAMERA],
        "allow_media_egress": True,
        "workflow": {"kind": "temporal"},
    },
    "temporal-tuned": {
        "repo_id": "local/fixture",
        "episodes": [3, 4, 5],
        "instruction": "Annotate the subtasks",
        "provider": "fixture",
        "cameras": [CAMERA, "observation.images.wrist"],
        "allow_media_egress": True,
        "budget": {"max_calls": 20, "max_tokens": None},
        "workflow": {
            "kind": "temporal",
            "refine": "auto",
            "max_evidence_frames": 200,
            "boundary_window_seconds": 0.8,
            "boundary_tolerance_seconds": 0.1,
        },
    },
    "review-default": {
        "repo_id": "local/fixture",
        "episodes": [0, 1],
        "instruction": "Review the motion",
        "provider": "fixture",
    },
    "review-anchored": {
        "repo_id": "local/fixture",
        "episodes": [0],
        "instruction": "Judge the release",
        "provider": "fixture",
        "cameras": ["observation.images.front", "observation.images.wrist"],
        "allow_media_egress": True,
        "workflow": {"anchored": {"spec": "plates-release"}},
    },
    "objects": {
        "repo_id": "local/fixture",
        "episodes": [0],
        "instruction": "Mask the plate",
        "provider": "fixture",
        "cameras": [CAMERA],
        "workflow": {"kind": "objects", "object_concepts": ["plate"]},
    },
}


def run_of(context):
    """A fixed run record around a normalised context: the fields
    ``planning.material`` reads, with constant values."""
    from levi.agent.observations import skills
    from levi.agent.schema import TaskContext
    from levi.agent.store import digest

    return {
        "context": context,
        "provider_config": {"name": "fixture", "kind": "openai-compatible"},
        "base_revision": "rev-0",
        "base_content": "0" * 64,
        "planned_hashes": {"meta/info.json": "1" * 64},
        "files": ["meta/info.json"],
        "skill_fingerprints": {
            k: digest(v) for k, v in skills(TaskContext.model_validate(context)).items()
        },
        "harness": {
            "parameters": {"evidence.refine_top_k": 2},
            "sources": {"evidence.refine_top_k": "fixture"},
            "memory": {"ignored": True},
        },
    }


def snapshot():
    from levi.agent.planning import clarify, material
    from levi.agent.schema import TaskContext
    from levi.agent.store import digest, dumps

    out = {}
    for name, raw in CONTEXTS.items():
        context = TaskContext.model_validate(raw)
        dumped = context.model_dump()
        # As runtime.model_step builds its cache fingerprint.
        cached = TaskContext.model_validate(dumped).model_dump(exclude={"budget"})
        out[name] = {
            "context": dumps(dumped),
            # Field order too, as json.dumps writes it (dumps sorts keys).
            "context_ordered": json.dumps(dumped, ensure_ascii=False),
            "context_digest": digest(dumped),
            "clarify_workflow": dumps(clarify(dumped)["workflow"]),
            "cache_context_digest": digest(cached),
            "material_digest": digest(material(run_of(dumped))),
        }
    return out


if __name__ == "__main__":
    json.dump(snapshot(), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
