"""The task-independent configuration: guideline, subtask definitions,
vocabulary and release-review spec, each filled in with the rollout's task
instruction.

The files live in ``levi/live/specs/`` and carry their version in the name
(``generic-guideline.v1.md``). A file that has been used is never edited in
place: a change is a new file with a new version and a new ``live.toml``
setting, so a run's record always says exactly which text it used.

Standard library only.
"""

import hashlib
import json
import re
from pathlib import Path

SPEC_DIR = Path(__file__).parent / "specs"
MAX_TASK = 600


def clean_task(text: str) -> str:
    """The task instruction as it is quoted to a model: one line, no control
    characters, bounded."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_TASK] or "(no task instruction recorded)"


def path(name: str) -> Path:
    target = (SPEC_DIR / name).resolve()
    if target.parent != SPEC_DIR.resolve():
        raise ValueError(f"{name!r} is not a file of levi/live/specs")
    return target


def text(name: str) -> str:
    return path(name).read_text(encoding="utf-8")


def sha256(name: str) -> str:
    return hashlib.sha256(path(name).read_bytes()).hexdigest()


def guideline(task: str, name: str = "generic-guideline.v1.md") -> str:
    """The annotation guideline with the task instruction quoted in it."""
    body = text(name)
    if "{task}" not in body:
        raise ValueError(f"{name} has no {{task}} placeholder")
    return body.replace("{task}", clean_task(task))


def definitions(name: str = "generic-definitions.v1.json") -> list:
    """Subtask definitions for a temporal plan (``workflow.definitions``)."""
    return json.loads(text(name))


def vocabulary(name: str = "generic-vocabulary.v1.json") -> list:
    """The subtasks written to a dataset's own vocabulary file."""
    return json.loads(text(name))["subtasks"]


def normalize_task(text: str | None) -> str:
    """A task text or folder name as the rewrite table is keyed: lower case,
    underscores as spaces, one space between words (the training pool's rule,
    ``levi.pool.rules.normalize_task``)."""
    from levi.pool.rules import normalize_task as pool_normalize

    return pool_normalize(text)


def judge_task(task: str, table: dict | None, folder: str | None = None):
    """``(text, how)``: the task instruction as the judgement quotes it, and
    how it got there. ``table`` is ``[judge.task_text]`` of ``live.toml``:
    normalized task text (or task folder name) to the wording the judgement
    uses. The first match wins, **the task folder's name before the
    instruction's text** (the folder is the more specific: one deployment can
    need a wording that the same instruction elsewhere does not). The result
    is never looked up again, so entries do not chain; an entry that says what
    the instruction already says ends the lookup too. ``how`` is ``None`` when
    the instruction is quoted as it is (no entry, or such an entry), else
    ``"task_folder"`` or ``"task_text"``. The robot policy never sees this
    text: only the judgements' questions quote it."""
    if table:
        keys = {normalize_task(k): v for k, v in table.items()}
        for how, probe in (("task_folder", folder), ("task_text", task)):
            key = normalize_task(probe) if probe else ""
            if key in keys:
                wording = clean_task(keys[key])
                return (wording, how) if wording != clean_task(task) else (task, None)
    return task, None


def anchored_spec(
    task: str, name: str = "generic-release.v1.json", min_valid=1
) -> dict:
    """The release-review spec with the task instruction in its question (and
    in its start check's, when that quotes it)."""
    spec = json.loads(text(name))
    if "{task}" not in spec["question"]:
        raise ValueError(f"{name}: the question has no {{task}} placeholder")
    spec["question"] = spec["question"].replace("{task}", clean_task(task))
    if spec.get("start"):
        spec["start"]["question"] = spec["start"]["question"].replace(
            "{task}", clean_task(task)
        )
    if min_valid != 1:
        spec["episode"] = {**spec.get("episode", {}), "min_valid": int(min_valid)}
    return spec


def manifest(config) -> dict:
    """Which texts a configuration uses, with their hashes (for records)."""
    p = config.pipeline
    # Review only (``pipeline.temporal = false``) uses no time-segment texts.
    files = (
        [p.guideline, p.vocabulary, "generic-definitions.v1.json"] if p.temporal else []
    )
    if p.anchored:
        files.append(p.anchored_spec)
    return {name: sha256(name) for name in files}
