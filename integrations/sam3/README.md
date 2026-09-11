# Optional SAM3 worker for LEVI

This directory is an **opt-in integration**, not part of LEVI's core runtime.
The core application stores model-neutral object annotations and remains usable
on CPU-only machines. The worker loads the official Meta SAM3 implementation
only when a user explicitly creates this environment and sets
`LEVI_SAM3_ENABLED=1`.

## Install on a CUDA machine

1. Request access to the checkpoint at <https://huggingface.co/facebook/sam3>
   (or `facebook/sam3.1`) and authenticate with `hf auth login`.
2. Use Python 3.12 or newer and a CUDA-enabled PyTorch build compatible with
the installed NVIDIA driver. From the LEVI repository root:

```bash
uv venv --python 3.12 integrations/sam3/.venv
uv sync --project integrations/sam3
uv run --project integrations/sam3 levi-sam3-worker --check
```

The exact SAM3 source is pinned in `pyproject.toml`; checkpoints and model
weights are never committed to this repository. The official SAM3 license and
access terms apply to the worker and its weights. See `../../NOTICE`,
`../../docs/UPSTREAM.md`, and `../../THIRD_PARTY_NOTICES.md`.

## Connect it to a running LEVI server

Set these variables before starting LEVI:

```bash
export LEVI_SAM3_ENABLED=1
export LEVI_SAM3_WORKER_PYTHON="$PWD/integrations/sam3/.venv/bin/python"
# Optional: use a local checkpoint instead of the Hugging Face download.
# export LEVI_SAM3_CHECKPOINT=/absolute/path/to/sam3.pt
uv run levi
```

The UI first creates a plan. The worker then reads one camera stream at a time,
adds text prompts on the selected keyframe, propagates masks through the
episode, and writes JSON back to LEVI's revisioned sidecar. The control plane
never overwrites source `meta/`, `data/`, or `videos/` files.

## Contract

`levi_sam3_worker` accepts a plan JSON and writes a result JSON. The plan uses
LeRobot episode indices and `observation.images.*` camera keys. Each result
contains `episode_index`, `frame_index`, `timestamp`, `camera_key`, `object_id`,
`track_id`, pixel `bbox_xyxy`, image dimensions, and lossless COCO uncompressed
RLE counts. Results are initially `suggested`; human review in LEVI creates a
new sidecar revision.

Do not run this optional worker in LEVI's CPU-only CI. The repository's tests
use the deterministic `provider=fake` path and never import Torch, CUDA, or
SAM3.
