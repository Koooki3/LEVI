"""The contract every input format implements.

To support a new kind of source data, subclass ``InputFormat``, implement
``detect``/``inspect``/``episodes``, and add an instance to
``registry.INPUT_FORMATS``. The contract tests in tests/test_formats.py then
exercise it against every registered output automatically.
"""

from pathlib import Path

from ..episodes import SourceEpisode
from ..options import Options
from ..report import InputReport


class InputFormat:
    id: str = ""
    label: str = ""
    description: str = ""
    # Strongest evidence this format works: "real-data", "fixture" or "none".
    evidence: str = "fixture"
    # Whether the conversion pipeline can read episodes from this format
    # (False for formats that are already a finished dataset).
    convertible: bool = True
    # A raw input may support a read-only browsing view without supporting
    # training-format conversion (for example, DROID needs an action mapping).
    viewable: bool = True

    def detect(self, root: Path) -> float:
        """Confidence in [0, 1] that ``root`` is this format (cheap)."""
        raise NotImplementedError

    def inspect(self, root: Path, options: Options, progress=None) -> InputReport:
        """Requirement checklist + summary, without decoding video."""
        raise NotImplementedError

    def episodes(self, root: Path, options: Options) -> list[SourceEpisode]:
        """Selected episodes, in output order, for the conversion pipeline."""
        raise NotImplementedError

    def describe(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "evidence": self.evidence,
            "convertible": self.convertible,
            "viewable": self.viewable,
        }
