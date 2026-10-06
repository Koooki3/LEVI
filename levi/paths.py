"""Portable paths configured solely by LEVI_WORKSPACE, defaulting to .state."""

import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
from dotenv import load_dotenv

load_dotenv(PROJECT / ".env", override=False)


def workspace() -> Path:
    explicit = os.getenv("LEVI_WORKSPACE")
    return Path(explicit).expanduser().resolve() if explicit else PROJECT / ".state"


ROOT = workspace()


def inside(path: str | Path, base: Path | None = None) -> Path:
    # The workspace is read when asked, not when this module was imported, so a
    # test (or a second workspace in one process) can point it elsewhere.
    base = ROOT if base is None else base
    result = Path(path).expanduser()
    if not result.is_absolute():
        result = base / result
    result = result.resolve()
    if not result.is_relative_to(base.resolve()):
        raise ValueError("Path must remain inside the configured workspace")
    return result


STATE = inside(ROOT / "outputs/LEVI/workbench")
CACHE = inside(ROOT / ".cache/levi")
EXPORTS = inside(ROOT / "outputs/LEVI/exports")
CHECKPOINTS = inside(ROOT / "checkpoints")
SAM3_CHECKPOINT_DIR = inside(CHECKPOINTS / "sam3")
# RECAP value-model checkpoints: one folder per imported checkpoint, each with
# a manifest.json (levi/recap/checkpoints.py).
RECAP_VALUE_CHECKPOINT_DIR = inside(CHECKPOINTS / "recap_value")


def configure() -> None:
    configured_checkpoint_dir = os.getenv("LEVI_SAM3_CHECKPOINT_DIR")
    if configured_checkpoint_dir:
        try:
            checkpoint_dir = inside(configured_checkpoint_dir)
        except ValueError as exc:
            raise ValueError(
                "LEVI_SAM3_CHECKPOINT_DIR must remain inside LEVI_WORKSPACE"
            ) from exc
    else:
        checkpoint_dir = SAM3_CHECKPOINT_DIR
    # Read where used (levi/recap/checkpoints.py); not exported, since the
    # RECAP worker is handed explicit paths. Checked here to fail early.
    configured_recap_dir = os.getenv("LEVI_RECAP_VALUE_CHECKPOINT_DIR")
    if configured_recap_dir:
        try:
            inside(configured_recap_dir)
        except ValueError as exc:
            raise ValueError(
                "LEVI_RECAP_VALUE_CHECKPOINT_DIR must remain inside LEVI_WORKSPACE"
            ) from exc
    values = {
        "UV_CACHE_DIR": ROOT / ".cache/uv",
        "HF_HOME": ROOT / ".cache/huggingface",
        "HF_HUB_CACHE": ROOT / ".cache/huggingface/hub",
        "BUN_INSTALL_CACHE_DIR": ROOT / ".cache/bun",
        "PLAYWRIGHT_BROWSERS_PATH": ROOT / ".cache/playwright",
        "TMPDIR": ROOT / "tmp/runtime/levi",
        "LEROBOT_ANNOTATE_CACHE": CACHE,
        "LEROBOT_ANNOTATE_EXPORT": EXPORTS,
        # Model weights are workspace-local and never part of the repository.
        "LEVI_SAM3_CHECKPOINT_DIR": checkpoint_dir,
    }
    new_workspace = not STATE.exists()
    for key, value in values.items():
        # LEVI subprocesses keep all managed data within their workspace.
        inside(value).mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(value)
    STATE.mkdir(parents=True, exist_ok=True)
    if new_workspace:
        # A new workspace is offered a DROID test sample; it is downloaded
        # only on request (`levi sample fetch droid`, or LEVI_DROID_SAMPLE=on
        # lets the service take it; levi/samples).
        from .samples import note_new_workspace

        note_new_workspace()
