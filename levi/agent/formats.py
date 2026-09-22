"""Extension registry: dataset readers, annotation semantics and export planning.

Adapters reuse project-owned readers/writers. New formats register capabilities;
callers do not add conditionals to the runtime, REST, MCP or review UI.
"""

import json
from dataclasses import dataclass
from typing import Protocol


class DatasetAdapter(Protocol):
    id: str

    def pin(self, context): ...
    def verify(self, context, manifest): ...
    def publication_state(self, context): ...
    def inspect(self, context): ...
    def snapshot(self, context, destination, files, hashes): ...
    def sample(self, context, root, episode, artifacts): ...


class LeRobotAdapter:
    id = "lerobot-and-raw-view"

    def pin(self, context):
        from . import media

        return media.pin_remote(context)

    def verify(self, context, manifest):
        from . import media

        return media.verify_source(context, manifest)

    def publication_state(self, context):
        from . import media

        return media.state_for(context)

    def inspect(self, context):
        from . import media

        return media.inspect(context)

    def snapshot(self, context, destination, files, hashes):
        from . import media

        return media.snapshot(context, destination, files, hashes)

    def sample(self, context, root, episode, artifacts):
        from . import media

        return media.sample(context, root, episode, artifacts)

    def sample_temporal(
        self, context, root, episode, artifacts, proposals=None, spacing=None
    ):
        from . import media
        from .observations import frame_scope

        return media.sample(
            context,
            root,
            episode,
            artifacts,
            frame_indices=frame_scope(context, root, episode, proposals, spacing),
        )


@dataclass(frozen=True)
class AnnotationKind:
    id: str
    layer: str
    native_format: str
    point: bool
    requires_human_outcome: bool = False

    def validate(self, proposal):
        if (
            self.layer == "language"
            and not self.point
            and proposal.style not in {"subtask", "plan", "memory", "task_aug"}
        ):
            raise ValueError("Persistent language style required")
        if not self.point and (proposal.end is None or proposal.end <= proposal.start):
            raise ValueError("Segments require a non-empty half-open interval")
        if self.point and proposal.end is not None:
            raise ValueError("Point proposals do not have an end")
        if self.requires_human_outcome and proposal.outcome is None:
            raise ValueError("Outcome proposals need an outcome (or unknown)")

    def paths(self, proposal):
        ep = proposal["episode_index"]
        if self.layer == "language":
            return [f"annotations/episode_{ep:06d}.json"]
        if self.layer == "outcome":
            return [f"annotations/outcomes/episode_{ep:06d}.json"]
        return ["review.json"]

    def apply(self, proposal, *, app, state, atoms, folder, origin=None):
        """Plugins may override validate/paths/apply together; no runtime branch."""
        ep = proposal["episode_index"]
        if self.layer == "language":
            atom = {
                "role": "assistant",
                "content": proposal["content"],
                "style": "interjection" if self.point else proposal["style"],
                "timestamp": proposal["start"],
                "camera": None,
            }
            if self.point:
                atom["timestamp"] = app._snap(
                    proposal["start"], app._frame_timestamps(state, ep)
                )
            else:
                atom["to"] = proposal["end"]
            # LEVI-only, like ``to``: what the reviewer approved beyond the
            # text -- which subtask, its outcome, the attempt and the stated
            # doubt -- and where it came from. The exported lerobot struct is
            # built from an explicit field list and never carries it.
            atom["levi"] = {
                key: proposal.get(key)
                for key in ("subtask_id", "outcome", "attempt", "uncertainty")
                if proposal.get(key) not in (None, "")
            }
            if origin:
                from .supersede import atom_key

                atom["levi"]["origin"] = {
                    **origin,
                    "kind": "agent",
                    "written": list(atom_key(atom)),
                }
            app._validate_atom(atom)
            atoms.append(atom)
        elif self.layer == "outcome":
            if proposal["outcome"] != "unknown":
                app.outcomes.write_label(state.annotations_dir, ep, proposal["outcome"])
        elif self.layer == "review":
            path = folder / "review.json"
            value = (
                json.loads(path.read_text())
                if path.exists()
                else {"flagged": [], "notes": ""}
            )
            value["flagged"] = sorted(set(value["flagged"] + [ep]))
            value["notes"] += "\n" + proposal["content"]
            app.atomic(path, value)
        else:
            raise ValueError("Annotation adapter does not implement this layer")


DATASETS: dict[str, DatasetAdapter] = {"lerobot-and-raw-view": LeRobotAdapter()}
ANNOTATIONS = {
    "segment": AnnotationKind("segment", "language", "language_persistent", False),
    "event": AnnotationKind("event", "language", "language_events", True),
    "issue": AnnotationKind("issue", "review", "levi.review.v1", True),
    "outcome": AnnotationKind("outcome", "outcome", "levi.outcome", True, True),
}


def register_dataset(adapter):
    if adapter.id in DATASETS:
        raise ValueError("Dataset adapter already registered")
    DATASETS[adapter.id] = adapter


def register_annotation(kind):
    if kind.id in ANNOTATIONS:
        raise ValueError("Annotation kind already registered")
    ANNOTATIONS[kind.id] = kind


def describe():
    from dataclasses import asdict

    return {
        "datasets": list(DATASETS),
        "annotation_kinds": [asdict(k) for k in ANNOTATIONS.values()],
        "objects": {
            "tool": "sam3",
            "format": "levi.sam3.sidecar.v1",
            "encoding": "COCO RLE + parquet",
        },
        "exports": list(EXPORTS),
        "conversion_registry": "levi.conversion.registry",
        "source_read_only": True,
    }


class ExportAdapter(Protocol):
    id: str

    def plan(self, run): ...


class NativeAnnotatedExport:
    id = "lerobot-native"

    def plan(self, run):
        from levi.catalog import datasets

        repo = run["context"]["repo_id"]
        entry = next((d for d in datasets().values() if d["id"] == repo), {})
        raw = entry.get("kind") == "raw"
        return {
            "target": self.id,
            "repo_id": repo,
            "source_read_only": True,
            "requires_committed_annotations": True,
            "ready": run["status"] in {"succeeded", "partially_succeeded"} and not raw,
            "method": "existing annotation export endpoint",
            "conversion_required": raw,
            "raw_capture": "Convert first; carry-over uses source frame ledgers",
            "review_policy": "Only human-approved Agent changes are published",
            "included": [
                "native language columns",
                "outcome labels",
                "lossless object sidecars",
                "Agent provenance",
            ],
        }


EXPORTS: dict[str, ExportAdapter] = {"lerobot-native": NativeAnnotatedExport()}


def register_export(adapter):
    if adapter.id in EXPORTS:
        raise ValueError("Export adapter already registered")
    EXPORTS[adapter.id] = adapter
