"""The contract every output format implements.

To add a target, subclass ``OutputFormat`` (usually ``LeRobotV21``, adding
columns and metadata files), declare its ``defaults`` and ``check`` rules,
and add an instance to ``registry.OUTPUT_FORMATS``. The pipeline does the
episode planning, video work, statistics, validation and atomic publishing.
"""

from pathlib import Path
from typing import Any, ClassVar

import numpy as np
from pydantic import BaseModel, ConfigDict

from ..options import Options
from ..report import InputReport, TargetCompatibility


class TargetOptions(BaseModel):
    """Per-target settings; subclasses add fields."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class OutputFormat:
    id: str = ""
    label: str = ""
    description: str = ""
    evidence: str = "fixture"
    options_model: type[TargetOptions] = TargetOptions
    # Pipeline options this target changes unless the user overrides them.
    defaults: ClassVar[dict[str, Any]] = {}
    # Input format ids this target can be produced from.
    inputs: tuple[str, ...] = ()
    # Input requirement ids whose warnings do not concern this target.
    ignored_warnings: tuple[str, ...] = ()

    def target_options(self, value: dict | None) -> TargetOptions:
        return self.options_model.model_validate(value or {})

    def check(self, report: InputReport, options: Options) -> TargetCompatibility:
        raise NotImplementedError

    # --- writer hooks, called by the pipeline -------------------------------

    def episode_columns(
        self, n: int, episode: dict, settings: TargetOptions
    ) -> dict[str, tuple[np.ndarray, dict]]:
        """Extra per-frame columns: name -> (values, info.json feature)."""
        return {}

    def episode_fields(self, episode: dict, settings: TargetOptions) -> dict:
        """Extra keys for this episode's ``meta/episodes.jsonl`` row."""
        return {}

    def finalize(
        self, root: Path, episodes: list[dict], info: dict, settings: TargetOptions
    ) -> None:
        """Write target-specific metadata files after all episodes exist."""

    def from_dataset(self, source: Path, target: Path, options: Options, progress=None):
        """Export from an existing dataset (inputs with ``convertible=False``)."""
        raise ValueError(f"{self.label} cannot be exported from an existing dataset")

    def describe(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "evidence": self.evidence,
            "defaults": self.defaults,
            "inputs": list(self.inputs),
            "options_schema": self.options_model.model_json_schema(),
        }
