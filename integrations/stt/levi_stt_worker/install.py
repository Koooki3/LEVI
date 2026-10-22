"""Install a speech model folder: pinned download (or copy from a local
snapshot), checksum of the weights against the pinned hash, then
``MANIFEST.sha256``. Never writes into the shared Hugging Face cache."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .common import MODELS
from .errors import StartupError
from .manifest import file_sha256, verify_manifest, write_manifest

WEIGHTS = "model.safetensors"


def _copy_snapshot(source: Path, target: Path) -> None:
    """Copy a local snapshot (symlinks followed, hidden entries skipped)."""
    target.mkdir(parents=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if path.is_dir():
            (target / relative).mkdir(parents=True, exist_ok=True)
        else:
            shutil.copy2(path, target / relative)


def _download(repo: str, revision: str, target: Path, scratch: Path) -> None:
    # The Hugging Face client reads these at import time; point its cache at a
    # scratch folder that is removed afterwards and let it go online.
    os.environ["HF_HOME"] = str(scratch)
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.pop("TRANSFORMERS_OFFLINE", None)
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=repo,
        revision=revision,
        local_dir=target,
        ignore_patterns=[".gitattributes"],
    )


def install_model(model_dir: Path, name: str, source: Path | None = None) -> Path:
    """Install ``name`` under ``model_dir`` and return its folder. An existing
    folder that verifies is left alone; one that does not is an error (it is
    never overwritten or deleted here)."""
    if name not in MODELS:
        raise ValueError(f"unknown model {name!r}; known: {', '.join(sorted(MODELS))}")
    repo, revision, weights_sha = MODELS[name]
    target = model_dir / name
    if target.exists():
        verify_manifest(target)
        return target
    model_dir.mkdir(parents=True, exist_ok=True)
    partial = model_dir / f"{name}.partial"
    scratch = model_dir / f".{name}.hf-scratch"
    for leftover in (partial, scratch):
        if leftover.exists():
            shutil.rmtree(leftover)
    try:
        if source is not None:
            _copy_snapshot(source, partial)
        else:
            _download(repo, revision, partial, scratch)
        shutil.rmtree(partial / ".cache", ignore_errors=True)
        if file_sha256(partial / WEIGHTS) != weights_sha:
            raise StartupError(
                "manifest_mismatch",
                f"{WEIGHTS} is not the pinned revision of {repo}.",
                3,
            )
        write_manifest(partial)
        partial.replace(target)
    finally:
        for leftover in (partial, scratch):
            if leftover.exists():
                shutil.rmtree(leftover)
    return target
