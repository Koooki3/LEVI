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
MODEL_TYPES = ("pi0", "pi05", "pi0_fast")
# Input-transform presets: what an RLinf robot/env type means for value
# inference. ``fr3_recap`` is the FR3 RECAP run (RLinf 807e5fdd plus local FR3
# patches, see docs/RECAP.md): its checkpoint_utils sends ``fr3_recap`` through
# the libero branch (InjectDefaultPrompt(None) + LiberoInputs, pi05), and
# compute_advantages' KEY_MAPPINGS["fr3_recap"] maps
# observation.images.view1 -> observation/image (base_0_rgb),
# observation.images.hand -> observation/wrist_image (left_wrist_0_rgb) and the
# LeRobot task -> prompt; right_wrist_0_rgb is zeros with its mask off. The
# model and advantage numbers are those of recap_fr3_value.yaml /
# recap_fr3_advantages.yaml. The return range belongs to a returns tag, not to
# the preset, and is given at import.
PRESETS: dict[str, dict[str, Any]] = {
    "fr3_recap": {
        "env_type": "fr3_recap",
        "model_type": "pi05",
        "views": {
            "base_0_rgb": "observation.images.view1",
            "left_wrist_0_rgb": "observation.images.hand",
            "right_wrist_0_rgb": None,
        },
        "critic_expert_variant": "gemma_1m",
        "num_bins": 201,
        "v_min": -1.0,
        "v_max": 0.0,
        "max_token_len": 200,
        "precision": "bfloat16",
        "action_dim": 7,
        "action_horizon": 10,
        # sft_recap and rollouts_recap were built from raw captures filtered by
        # filter_static_pose_frames.py (5 mm / 0.01 rad, ±2 frames around a
        # gripper command flip kept).
        "static_filter": {
            "rule": "fr3_static_pose_v3",
            "xyz_threshold_m": 0.005,
            "euler_threshold_rad": 0.01,
            "gripper_epsilon": 1e-6,
            "gripper_protect_margin": 2,
            "min_frames": 2,
        },
        "gamma": 1.0,
        "failure_reward": -300.0,
        "lookahead": 10,
        "positive_quantile": 0.3,
    },
}
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


class StaticFilter(BaseModel):
    """The static-pose frame filter the model's training data went through
    (``levi/recap/static_filter.py``). A run on an unfiltered raw-capture
    view applies it by default: values and advantages over the kept frames
    only, the dropped frames left unlabelled."""

    model_config = ConfigDict(extra="forbid")
    rule: Literal["fr3_static_pose_v3"] = "fr3_static_pose_v3"
    xyz_threshold_m: float = Field(default=0.005, gt=0)
    euler_threshold_rad: float = Field(default=0.01, gt=0)
    gripper_epsilon: float = Field(default=1e-6, ge=0)
    gripper_protect_margin: int = Field(default=2, ge=0, le=100)
    min_frames: int = Field(default=2, ge=1)


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
    # RLinf robot/env type and openpi variant of the training run (the
    # ``fr3_recap`` preset sets both); ``model_type`` decides the mask of a
    # zero-padded camera slot (on only for pi0_fast).
    env_type: str | None = Field(default=None, max_length=64)
    model_type: Literal[MODEL_TYPES] = "pi05"  # type: ignore[valid-type]
    # RLinf config fields passed to ValueCriticConfig (from_checkpoint
    # defaults 32 / 50; not used by the value forward).
    action_dim: int = Field(default=32, ge=1, le=1024)
    action_horizon: int = Field(default=50, ge=1, le=10000)
    # Set when the training datasets were static-filtered (see StaticFilter).
    static_filter: StaticFilter | None = None
    # Advantage fields (RLinf compute_advantages).
    return_min: float | None = None
    return_max: float | None = None
    gamma: float = Field(default=1.0, gt=0, le=1)
    failure_reward: float = Field(default=-300.0, le=0)
    lookahead: int = Field(default=10, ge=1, le=10000)
    positive_quantile: float = Field(default=0.3, gt=0, lt=1)
    unified_threshold: float | None = None
    # Where each number came from (e.g. unified_threshold: "recomputed
    # locally on sft_recap+rollouts_recap", returns_tag: "fr3_r1_fail300").
    provenance: dict[str, str] = Field(default_factory=dict)
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
    inferred = (manifest.inspection or {}).get("inferred_variant")
    if (
        inferred
        and manifest.critic_expert_variant
        and inferred != manifest.critic_expert_variant
    ):
        found.append(
            f"critic_expert_variant is {manifest.critic_expert_variant} but the "
            f"weights have the shape of {inferred}"
        )
    preset = PRESETS.get(manifest.env_type or "")
    if preset:
        views = manifest.views.model_dump()
        if views != preset["views"]:
            found.append(
                f"views {views} differ from the {manifest.env_type} input transform "
                f"{preset['views']}"
            )
        if manifest.model_type != preset["model_type"]:
            found.append(
                f"model_type {manifest.model_type} differs from the "
                f"{manifest.env_type} run ({preset['model_type']})"
            )
    return found


def base_models_status(folder: Path, manifest: Manifest) -> dict[str, Any]:
    """Each base-model folder's import record (verified against the
    official release or a package hash list, or development-only)."""
    from . import base_models

    out = {}
    for key in ("siglip", "gemma3", "tokenizer"):
        try:
            path = base_model_path(folder, getattr(manifest.base_models, key))
        except ValueError:
            path = None
        out[key] = base_models.describe(path)
    return out


def dev_only(folder: Path, manifest: Manifest) -> list[str]:
    """Base models that are not verified official files (results made with
    them are for development only)."""
    if manifest.provider == "fake":
        return []
    return [
        key
        for key, info in base_models_status(folder, manifest).items()
        if info is not None and info.get("dev_only") is not False
    ]


def entry(folder: Path, manifest: Manifest) -> dict[str, Any]:
    issues = problems(folder, manifest)
    return {
        "name": manifest.name,
        "provider": manifest.provider,
        "ready": not issues,
        "reason": "; ".join(issues) or None,
        "dev_only_base_models": dev_only(folder, manifest),
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
                    "dev_only_base_models": [],
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
    "static_filter",
    "env_type",
    "model_type",
    "action_dim",
    "action_horizon",
    "provenance",
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
        if key in ("views", "base_models", "provenance") and isinstance(value, dict):
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
    preset: str | None = None,
) -> dict[str, Any]:
    """Copy (or hard-link) a checkpoint into the store and write its
    manifest. Refuses an existing name; the source is only read. A
    ``preset`` (``PRESETS``) fills the run's fields; explicit ``fields``
    win."""
    folder = checkpoint_dir(name)
    if folder.exists():
        raise FileExistsError(
            f"Checkpoint {name!r} already exists; choose another name or edit "
            "it with `levi recap set`"
        )
    fields = dict(fields or {})
    if preset is not None:
        if preset not in PRESETS:
            raise ValueError(f"Unknown preset {preset!r}; known: {sorted(PRESETS)}")
        merged = json.loads(json.dumps(PRESETS[preset]))
        for key, value in fields.items():
            if key in ("views", "base_models", "provenance") and isinstance(
                value, dict
            ):
                merged[key] = {**merged.get(key, {}), **value}
            else:
                merged[key] = value
        fields = merged
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


def strict_check(name: str, timeout: float = 1800) -> dict[str, Any]:
    """Build the model the manifest describes in the worker environment (on
    the CPU) and load the weights: RLinf's missing / unexpected key counts.
    The answer is kept in ``inspection.strict``."""
    folder, manifest = load(name)
    issues = [p for p in problems(folder, manifest) if "unified" not in p]
    if manifest.provider != "rlinf":
        raise ValueError("Only an rlinf checkpoint has weights to check")
    if issues:
        raise ValueError(f"Checkpoint {name} is not ready: " + "; ".join(issues))
    python = worker_python()
    project = paths.PROJECT / "integrations" / "recap_value"
    done = subprocess.run(
        [str(python), "-m", "levi_recap_worker.cli", "--strict-check", str(folder)],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        check=False,
        env={
            **os.environ,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "CUDA_VISIBLE_DEVICES": "",
        },
    )
    try:
        report = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ValueError(
            "strict check printed no JSON: " + (done.stderr or done.stdout)[-1500:]
        ) from None
    inspection = dict(manifest.inspection or {})
    inspection["strict"] = {**report, "checked_at": time.time()}
    write_manifest(folder, manifest.model_copy(update={"inspection": inspection}))
    return {"name": name, **report}
