"""The launch wizard's form becomes a new job file (T-API-1).

``POST /api/levi/automatic/jobs`` takes the form of the contract
(``levi2/design/aeri-ui-api-contract.md`` §2) and this module turns it into
a ``levi.aeri.job.v1`` file under the first job root, in its ``wizard/``
folder. Rules:

- a file is **never overwritten**: it is written whole to a hidden temporary
  file, checked by the real job loader (``cli.load_job``: schema, reset-mode
  rules, termination values) and only then linked into place, which fails
  when the name is taken (``job_exists``);
- the file holds no path of this machine: no rollout root, no contract path
  (a dry run writes into ``$LEVI_AERI_HOME``); the checkpoint ids chosen in
  the form are kept as comments (the job schema has no field for them);
- the strict YAML subset the loader reads is written with JSON-quoted text,
  so a text can never inject a key or a comment;
- ``human_assisted`` writes no reset policy and no reset instruction.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from levi.domain import aeri

from . import cli

FOLDER = "wizard"
SAFE = re.compile(r"[^A-Za-z0-9_.:-]")
ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


class FormError(Exception):
    """A form that cannot become a job: ``code`` (``job_invalid``,
    ``job_exists``, ``no_job_root``, ``job_write_failed``), the HTTP status,
    a sentence and, for ``job_invalid``, one ``{field, message}`` per
    problem."""

    def __init__(self, status: int, code: str, message: str, errors=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.errors = errors or []


class _Form(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TaskForm(_Form):
    instruction: str = Field(min_length=1, max_length=aeri.MAX_TEXT)
    reset_instruction: str | None = Field(None, min_length=1, max_length=aeri.MAX_TEXT)


class PolicyForm(_Form):
    checkpoint_id: str = Field(pattern=ID)


class ResetForm(_Form):
    strategy: Literal["human_assisted", "single_reset_policy"]
    scene_check: Literal["provider", "operator_attested"] = "provider"


class RunForm(_Form):
    episodes: int = Field(ge=0, le=100_000)
    max_steps: int = Field(ge=1, le=1_000_000)


class TerminationForm(_Form):
    allow_early_stop: bool | None = None


class RecordingForm(_Form):
    group: str | None = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class JobForm(_Form):
    request_id: str = Field(pattern=ID)
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$")
    task: TaskForm
    policy_forward: PolicyForm
    policy_reset: PolicyForm | None = None
    reset: ResetForm
    run: RunForm
    termination: TerminationForm | None = None
    recording: RecordingForm | None = None

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value in (".", ".."):
            raise ValueError("not a name")
        return value


def parse_form(body) -> JobForm:
    """The form, or ``FormError(422, job_invalid)`` with one error per
    offending field."""
    try:
        form = JobForm.model_validate(body)
    except ValidationError as exc:
        errors = [
            {
                "field": ".".join(str(p) for p in item["loc"]),
                "message": str(item["msg"])[:300],
            }
            for item in exc.errors()
        ]
        raise FormError(422, "job_invalid", "The form is not valid", errors) from None
    if form.reset.strategy == "human_assisted" and form.policy_reset is not None:
        raise FormError(
            422,
            "job_invalid",
            "The form is not valid",
            [
                {
                    "field": "policy_reset",
                    "message": "a person resets the scene: leave the reset policy out",
                }
            ],
        )
    if form.reset.strategy == "single_reset_policy" and form.policy_reset is None:
        raise FormError(
            422,
            "job_invalid",
            "The form is not valid",
            [{"field": "policy_reset", "message": "this reset strategy needs one"}],
        )
    return form


def _text(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _label(value) -> str:
    return SAFE.sub("?", str(value))[:128]


def build_text(form: JobForm, forward: dict, reset: dict | None) -> str:
    """The job file's text. ``forward`` / ``reset``: ``{id, config}`` of the
    chosen checkpoints (kept as comments)."""
    human = form.reset.strategy == "human_assisted"
    lines = [
        "# Written by the LEVI job wizard; the job schema has no policy field, so the",
        "# chosen checkpoints are recorded here only.",
        f"# forward policy: {_label(forward['id'])} (config {_label(forward.get('config'))})",
    ]
    if reset is not None:
        lines.append(
            f"# reset policy: {_label(reset['id'])} (config {_label(reset.get('config'))})"
        )
    lines += [
        f"schema_version: {aeri.JOB_SCHEMA}",
        "experiment:",
        f"  name: {form.name}",
        f"  episodes: {form.run.episodes}",
        "  random_seed: 0",
        f"  execution_mode: {'assisted' if human else 'autonomous'}",
        "policies:",
        "  forward:",
        f"    max_steps: {form.run.max_steps}",
    ]
    if not human:
        lines += ["  reset:", f"    max_steps: {form.run.max_steps}"]
    lines += ["task:", f"  instruction: {_text(form.task.instruction)}"]
    if not human and form.task.reset_instruction:
        lines.append(f"  reset_instruction: {_text(form.task.reset_instruction)}")
    if form.termination and form.termination.allow_early_stop is not None:
        lines += [
            "termination:",
            f"  allow_early_stop: {'true' if form.termination.allow_early_stop else 'false'}",
        ]
    lines += [
        "reset:",
        f"  strategy: {form.reset.strategy}",
        f"  scene_check: {form.reset.scene_check}",
    ]
    if form.recording and form.recording.group:
        lines += ["recording:", f"  group: {form.recording.group}"]
    return "\n".join(lines) + "\n"


def _write_new(folder: Path, name: str, data: bytes) -> bool:
    """Write ``folder/name`` only if nothing is there, whole or not at all,
    after the real loader accepted it. ``False``: the name is taken."""
    temporary = folder / f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            cli.load_job(temporary)
        except cli.JobError as exc:
            message = str(exc).replace(str(temporary), name)
            raise FormError(
                422,
                "job_invalid",
                "The job file would not load",
                [{"field": None, "message": message[:400]}],
            ) from None
        try:
            os.link(temporary, folder / name)
        except FileExistsError:
            return False
        return True
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        _sync(folder)


def _sync(folder: Path) -> None:
    with contextlib.suppress(OSError):
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_job(root: Path, form: JobForm, forward: dict, reset: dict | None) -> str:
    """Write the job under ``root/wizard/``; returns its name relative to
    the root (``wizard/<name>.yaml``). The same form written twice is the
    same file (idempotent); another form under a used name is
    ``job_exists``."""
    text = build_text(form, forward, reset)
    folder = root / FOLDER
    try:
        with contextlib.suppress(FileExistsError):
            folder.mkdir(mode=0o700, parents=True)
        if folder.is_symlink() or not folder.is_dir():
            raise FormError(
                503, "job_write_failed", f"{FOLDER}/ is not a folder in the job root"
            )
        name = f"{form.name}.yaml"
        if not _write_new(folder, name, text.encode()):
            try:
                same = (folder / name).read_text() == text
            except OSError:
                same = False
            if not same:
                raise FormError(
                    409, "job_exists", f"A job named {form.name} exists already"
                )
    except OSError as exc:
        raise FormError(
            503, "job_write_failed", f"The job root is not writable ({exc.strerror})"
        ) from None
    return f"{FOLDER}/{name}"
