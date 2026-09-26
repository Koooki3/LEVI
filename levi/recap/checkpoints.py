"""The RECAP value checkpoint store: one folder per checkpoint.

``$LEVI_WORKSPACE/checkpoints/recap_value/<name>/`` holds the weights LEVI
copied (or hard-linked) from a training run and a ``manifest.json`` that
states every choice the worker must not guess: the value-expert variant, the
camera mapping, the token length, the return range used for normalisation,
the threshold and where the base-model configs and tokenizer live. Importing
never moves or deletes the source; status and readiness checks only read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .. import paths

SCHEMA = "levi.recap_value.checkpoint.v1"
MANIFEST = "manifest.json"
WEIGHTS = "full_weights.pt"
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
# RLinf configuration.get_config variants (commit 807e5fd).
VARIANTS = (
    "dummy",
    "gemma_1m",
    "gemma_50m",
    "gemma_100m",
    "gemma_150m",
    "gemma_300m",
    "gemma_2b",
)
VIEW_SLOTS = ("base_0_rgb", "left_wrist_0_rgb", "right_wrist_0_rgb")
TOKENIZER_FILES = ("tokenizer.json", "tokenizer.model", "tokenizer_config.json")


class Views(BaseModel):
    """Which dataset camera feeds each of the model's three image slots.

    ``None`` is a zero image with its mask off, as RLinf's input transforms
    pad a missing camera (LiberoInputs keeps a wrist view, FrankaEEInputs
    pads both wrist slots)."""

    model_config = ConfigDict(extra="forbid")
    base_0_rgb: str | None = None
    left_wrist_0_rgb: str | None = None
    right_wrist_0_rgb: str | None = None

    @field_validator("*")
    @classmethod
    def _camera(cls, value):
        if value is not None and not value.startswith("observation.images."):
            raise ValueError("a view is an observation.images.* feature key")
        return value


class BaseModels(BaseModel):
    """Directories with the base configs (and optionally weights): SigLIP2
    so400m-patch14-224, Gemma3 270M and the Gemma3 tokenizer. Relative paths
    are inside the checkpoint folder; absolute ones stay inside the workspace."""

    model_config = ConfigDict(extra="forbid")
    siglip: str | None = None
    gemma3: str | None = None
    tokenizer: str | None = None


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["levi.recap_value.checkpoint.v1"] = Field(SCHEMA, alias="schema")
    name: str = Field(pattern=NAME_PATTERN)
    provider: Literal["rlinf", "fake"]
    weights: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)
    step: int | None = Field(default=None, ge=0)
    source: str | None = None
    imported_at: float
    notes: str = Field(default="", max_length=2000)
    # Model fields: the value expert's Gemma size. RLinf's training yaml
    # default (gemma_1m) and its from_checkpoint default (gemma_100m) differ,
    # so this is never assumed; `levi recap import` fills it from the weight
    # shapes when the worker environment can read them.
    critic_expert_variant: Literal[VARIANTS] | None = None  # type: ignore[valid-type]
    variant_source: Literal["given", "inferred"] | None = None
    num_bins: int = Field(default=201, ge=2, le=10001)
    v_min: float = -1.0
    v_max: float = 0.0
    max_token_len: int = Field(default=200, ge=8, le=4096)
    precision: Literal["bfloat16", "float32"] = "bfloat16"
    views: Views = Field(default_factory=Views)
    # Advantage fields (RLinf compute_advantages).
    return_min: float | None = None
    return_max: float | None = None
    gamma: float = Field(default=1.0, gt=0, le=1)
    failure_reward: float = Field(default=-300.0, le=0)
    lookahead: int = Field(default=10, ge=1, le=10000)
    positive_quantile: float = Field(default=0.3, gt=0, lt=1)
    unified_threshold: float | None = None
    base_models: BaseModels = Field(default_factory=BaseModels)
    # What the worker read from the weights at import (key count, inferred
    # variant); informational.
    inspection: dict[str, Any] | None = None

    @field_validator("weights")
    @classmethod
    def _relative(cls, value):
        if value is not None and (
            Path(value).is_absolute() or ".." in Path(value).parts
        ):
            raise ValueError("weights is a file name inside the checkpoint folder")
        return value

    def public(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


# ---------------------------------------------------------------- locations


def store_dir() -> Path:
    configured = os.environ.get("LEVI_RECAP_VALUE_CHECKPOINT_DIR")
    if not configured:
        return paths.RECAP_VALUE_CHECKPOINT_DIR
    try:
        return paths.inside(configured)
    except ValueError as exc:
        raise ValueError(
            "LEVI_RECAP_VALUE_CHECKPOINT_DIR must remain inside LEVI_WORKSPACE"
        ) from exc


def checkpoint_dir(name: str) -> Path:
    if not re.fullmatch(NAME_PATTERN, name or ""):
        raise ValueError(
            "A checkpoint name is 1-64 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return store_dir() / name


def base_model_path(folder: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        return (folder / path).resolve()
    return paths.inside(path)


# ---------------------------------------------------------------- reading


def load(name: str) -> tuple[Path, Manifest]:
    folder = checkpoint_dir(name)
    path = folder / MANIFEST
    if not path.is_file():
        raise KeyError(f"Unknown RECAP value checkpoint {name!r}")
    try:
        manifest = Manifest.model_validate(json.loads(path.read_text()))
    except (OSError, ValueError, ValidationError) as exc:
        raise ValueError(f"Invalid manifest for checkpoint {name!r}: {exc}") from exc
    if manifest.name != name:
        raise ValueError(
            f"Manifest of {name!r} names {manifest.name!r}; rename one of them"
        )
    return folder, manifest


def problems(folder: Path, manifest: Manifest) -> list[str]:
    """Everything that stops the worker running this checkpoint (read-only;
    the weights are size-checked here, hashed by ``levi recap checkpoints
    --verify``)."""
    if manifest.provider == "fake":
        return []
    found = []
    if not manifest.weights:
        found.append("no weights file in the manifest")
    else:
        weights = folder / manifest.weights
        if not weights.is_file():
            found.append(f"weights file {manifest.weights} is missing")
        elif (
            manifest.size_bytes is not None
            and weights.stat().st_size != manifest.size_bytes
        ):
            found.append(f"weights file {manifest.weights} changed size since import")
    if manifest.critic_expert_variant is None:
        found.append(
            "critic_expert_variant is not set (RLinf defaults disagree: "
            "gemma_1m in the training yaml, gemma_100m in from_checkpoint)"
        )
    if not manifest.views.base_0_rgb:
        found.append("views.base_0_rgb is not set (which camera is the base view)")
    for key, required in (
        ("siglip", ("config.json",)),
        ("gemma3", ("config.json",)),
        ("tokenizer", TOKENIZER_FILES),
    ):
        value = getattr(manifest.base_models, key)
        try:
            path = base_model_path(folder, value)
        except ValueError:
            found.append(f"base_models.{key} is outside the workspace")
            continue
        if path is None:
            found.append(f"base_models.{key} is not set")
        elif not path.is_dir() or not any((path / f).is_file() for f in required):
            found.append(f"base_models.{key} has no {' / '.join(required)} at {path}")
    if manifest.v_min >= manifest.v_max:
        found.append("v_min must be below v_max")
    return found


def entry(folder: Path, manifest: Manifest) -> dict[str, Any]:
    issues = problems(folder, manifest)
    return {
        "name": manifest.name,
        "provider": manifest.provider,
        "ready": not issues,
        "reason": "; ".join(issues) or None,
        "created_at": manifest.imported_at,
        "step": manifest.step,
        "notes": manifest.notes,
    }


def listing() -> list[dict[str, Any]]:
    """Every checkpoint with its readiness, newest first. A folder with a
    broken manifest is listed as not ready rather than hidden."""
    root = store_dir()
    rows = []
    if not root.is_dir():
        return rows
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or not (folder / MANIFEST).is_file():
            continue
        try:
            _folder, manifest = load(folder.name)
        except (KeyError, ValueError) as exc:
            rows.append(
                {
                    "name": folder.name,
                    "provider": "rlinf",
                    "ready": False,
                    "reason": str(exc),
                    "created_at": folder.stat().st_mtime,
                    "step": None,
                    "notes": "",
                }
            )
            continue
        rows.append(entry(folder, manifest))
    rows.sort(key=lambda r: -(r["created_at"] or 0))
    return rows


# ---------------------------------------------------------------- import


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(16 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_source(source: Path) -> tuple[Path, int | None]:
    """The ``full_weights.pt`` behind a ``global_step_*`` folder, its
    ``actor`` folder, its ``model_state_dict`` folder or the file itself,
    and the training step when the path names one."""
    source = Path(source).expanduser().resolve()
    candidates = [
        source,
        source / WEIGHTS,
        source / "model_state_dict" / WEIGHTS,
        source / "actor" / "model_state_dict" / WEIGHTS,
    ]
    weights = next((c for c in candidates if c.is_file()), None)
    if weights is None:
        raise FileNotFoundError(
            f"No {WEIGHTS} at {source} (expected a global_step_* folder, its "
            "actor folder or the file)"
        )
    step = None
    for part in reversed(weights.parts):
        match = re.fullmatch(r"global_step_(\d+)", part)
        if match:
            step = int(match.group(1))
            break
    return weights, step


def _place(source: Path, target: Path) -> str:
    """Hard-link when on one filesystem, else copy; never touches ``source``."""
    temp = target.with_name(f".{target.name}.{time.time_ns()}.partial")
    try:
        os.link(source, temp)
        how = "hardlink"
    except OSError:
        shutil.copy2(source, temp)
        how = "copy"
    os.replace(temp, target)
    return how


def write_manifest(folder: Path, manifest: Manifest) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / f".{MANIFEST}.{time.time_ns()}.tmp"
    temp.write_text(json.dumps(manifest.public(), indent=2, allow_nan=False) + "\n")
    os.replace(temp, folder / MANIFEST)


def worker_python() -> Path:
    project = paths.PROJECT / "integrations" / "recap_value"
    return Path(
        os.environ.get(
            "LEVI_RECAP_VALUE_WORKER_PYTHON", str(project / ".venv/bin/python")
        )
    ).expanduser()


def inspect_weights(weights: Path, timeout: float = 600) -> dict[str, Any]:
    """Ask the worker environment what the weights contain (key count,
    inferred expert variant). Never raises: an unreadable answer is returned
    as ``{"error": ...}``."""
    python = worker_python()
    project = paths.PROJECT / "integrations" / "recap_value"
    if not python.is_file():
        return {"error": f"worker environment not found at {python}"}
    try:
        done = subprocess.run(
            [str(python), "-m", "levi_recap_worker.cli", "--inspect", str(weights)],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": f"inspection did not run: {exc}"}
    if done.returncode != 0:
        return {"error": (done.stderr or done.stdout).strip()[-1000:]}
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "inspection printed no JSON"}


EDITABLE = (
    "critic_expert_variant",
    "num_bins",
    "v_min",
    "v_max",
    "max_token_len",
    "precision",
    "views",
    "return_min",
    "return_max",
    "gamma",
    "failure_reward",
    "lookahead",
    "positive_quantile",
    "unified_threshold",
    "base_models",
    "notes",
    "step",
)


def _merge(manifest: Manifest, fields: dict[str, Any]) -> Manifest:
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise ValueError(f"Not editable manifest fields: {sorted(unknown)}")
    data = manifest.public()
    for key, value in fields.items():
        if key in ("views", "base_models") and isinstance(value, dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    if "critic_expert_variant" in fields:
        data["variant_source"] = "given" if fields["critic_expert_variant"] else None
    return Manifest.model_validate(data)


def _check_base_models(folder: Path, manifest: Manifest) -> None:
    for key in ("siglip", "gemma3", "tokenizer"):
        base_model_path(folder, getattr(manifest.base_models, key))


def import_checkpoint(
    source: Path | None,
    name: str,
    *,
    provider: str = "rlinf",
    fields: dict[str, Any] | None = None,
    inspect: bool = True,
) -> dict[str, Any]:
    """Copy (or hard-link) a checkpoint into the store and write its
    manifest. Refuses an existing name; the source is only read."""
    folder = checkpoint_dir(name)
    if folder.exists():
        raise FileExistsError(
            f"Checkpoint {name!r} already exists; choose another name or edit "
            "it with `levi recap set`"
        )
    fields = dict(fields or {})
    base = {"name": name, "provider": provider, "imported_at": time.time()}
    weights = step = None
    if provider == "rlinf":
        if source is None:
            raise ValueError("An rlinf checkpoint needs its source path")
        weights, step = resolve_source(source)
    elif source is not None:
        raise ValueError("A fake checkpoint has no weights to import")
    manifest = _merge(Manifest.model_validate(base), fields)
    if step is not None and manifest.step is None:
        manifest = manifest.model_copy(update={"step": step})
    staging = folder.with_name(f".{name}.{time.time_ns()}.partial")
    staging.mkdir(parents=True)
    try:
        how = None
        if weights is not None:
            how = _place(weights, staging / WEIGHTS)
            manifest = manifest.model_copy(
                update={
                    "weights": WEIGHTS,
                    "sha256": sha256(staging / WEIGHTS),
                    "size_bytes": (staging / WEIGHTS).stat().st_size,
                    "source": str(weights),
                }
            )
            if inspect:
                found = inspect_weights(staging / WEIGHTS)
                update: dict[str, Any] = {"inspection": found}
                inferred = found.get("inferred_variant")
                if manifest.critic_expert_variant is None and inferred in VARIANTS:
                    update |= {
                        "critic_expert_variant": inferred,
                        "variant_source": "inferred",
                    }
                manifest = manifest.model_copy(update=update)
        _check_base_models(folder, manifest)
        Manifest.model_validate(manifest.public())
        write_manifest(staging, manifest)
        os.replace(staging, folder)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        **entry(folder, manifest),
        "path": str(folder),
        "placed_by": how,
        "manifest": manifest.public(),
    }


def update(name: str, fields: dict[str, Any]) -> dict[str, Any]:
    folder, manifest = load(name)
    manifest = _merge(manifest, fields)
    _check_base_models(folder, manifest)
    write_manifest(folder, manifest)
    return {**entry(folder, manifest), "manifest": manifest.public()}


def verify(name: str) -> dict[str, Any]:
    """Re-hash the weights against the manifest (reads the whole file)."""
    folder, manifest = load(name)
    if not manifest.weights:
        return {"name": name, "ok": True, "detail": "no weights"}
    path = folder / manifest.weights
    if not path.is_file():
        return {"name": name, "ok": False, "detail": "weights missing"}
    actual = sha256(path)
    return {
        "name": name,
        "ok": actual == manifest.sha256,
        "detail": "sha256 matches" if actual == manifest.sha256 else "sha256 differs",
    }
