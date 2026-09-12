# SAM3 worker for LEVI

This directory contains LEVI's isolated SAM3 worker. The capability is enabled
globally by the core application, while the worker itself runs in its own uv
environment so the CPU-safe workbench never imports Torch or probes CUDA.

The default model is the checkpoint mirror 1038lab/sam3, file sam3.pt. Model
weights are downloaded only when the first real SAM3 job starts and are saved
under the active LEVI_WORKSPACE:

~~~text
$LEVI_WORKSPACE/checkpoints/sam3/sam3.pt
~~~

The annotation page reports the current Hugging Face account, worker status,
download bytes/progress and checkpoint path. It works for built-in demos,
Hub datasets and registered local datasets.

## Install on the CUDA host

From the cloned LEVI root:

1. Open <https://huggingface.co/1038lab/sam3>. Request access if the model page
   is gated.
2. Authenticate as the account that can read the model:

~~~bash
hf auth login
hf auth whoami
~~~

   When hf is not installed globally, use uvx hf auth login and uvx hf auth
   whoami. A browser token can also be entered through LEVI's Connect Hugging
   Face dialog.
3. Create and sync the isolated worker:

~~~bash
uv venv --python 3.12 integrations/sam3/.venv
uv sync --project integrations/sam3
export LEVI_SAM3_WORKER_PYTHON="$PWD/integrations/sam3/.venv/bin/python"
~~~

4. Configure the model and run the model-free checks:

~~~bash
export LEVI_SAM3_ENABLED=1
export LEVI_SAM3_MODEL_REPO=1038lab/sam3
export LEVI_SAM3_MODEL_FILENAME=sam3.pt
export LEVI_SAM3_MODEL_REVISION=main
uv run --project integrations/sam3 levi-sam3-worker --check
uv run levi sam3 check
~~~

The checks print model_imported=0 and cuda_probe_performed=0. They do not
download a checkpoint or run inference. The real worker loads Torch, checks
CUDA and downloads the checkpoint only when a SAM3 annotation job is started
on the configured host.

## Start LEVI

~~~bash
uv run levi build
uv run levi serve
~~~

Open http://127.0.0.1:7860. Select an episode from either a demo or local
dataset, open the annotation tab, verify the account in the SAM3 runtime card,
choose a camera and enter prompts such as cup, plate, robot gripper. Click
Run SAM3 annotation. The first job downloads the checkpoint; later jobs reuse
the workspace file.

The worker receives the current browser cookie token for one subprocess only.
A CLI login or HF_TOKEN is used as a fallback. Tokens are never written to
plan JSON, job JSON, logs, sidecars or Git.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| LEVI_SAM3_ENABLED | 1 | Global SAM3 action switch; set 0 to disable |
| LEVI_SAM3_MODEL_REPO | 1038lab/sam3 | Compatible Hub model repository |
| LEVI_SAM3_MODEL_FILENAME | sam3.pt | Checkpoint file |
| LEVI_SAM3_MODEL_REVISION | main | Hub revision |
| LEVI_SAM3_CHECKPOINT_DIR | $LEVI_WORKSPACE/checkpoints/sam3 | Workspace-local cache |
| LEVI_SAM3_CHECKPOINT | empty | Existing local checkpoint; skips Hub download |
| LEVI_SAM3_WORKER_PYTHON | integrations/sam3/.venv/bin/python | Worker interpreter |
| LEVI_SAM3_DOWNLOAD_VIDEOS | 1 | Prepare Hub video assets before a real job; set 0 only when prepared externally |
| HF_TOKEN | empty | Optional server-side read token |

The worker downloads with huggingface_hub.hf_hub_download and monitors
Hugging Face's incomplete file while it writes a JSON progress record named
download-progress.json beside the checkpoint. The control plane reads that
record without importing Torch. The initial size is fetched from model
metadata when available; otherwise the UI uses an indeterminate progress bar.

The official SAM3 predictor is called with an explicit checkpoint_path. This
prevents the upstream builder from silently looking up facebook/sam3 and makes
the 1038lab mirror the single configured source. A pre-downloaded compatible
checkpoint can be selected with LEVI_SAM3_CHECKPOINT.

## Contract and sidecar

The worker accepts a staged plan containing LeRobot episode indices,
observation.images.* camera keys and text prompts. It returns JSON rows with
episode_index, frame_index, timestamp, camera_key, object_id, track_id,
concept, category, pixel bbox_xyxy, image dimensions, score, visibility and
lossless COCO uncompressed RLE. Results are initially suggested. Human review
creates a new sidecar revision outside the source dataset.

The source implementation is pinned in pyproject.toml. Checkpoints and model
weights are never committed. The official SAM3 license and model access terms
apply; see ../../NOTICE, ../../docs/UPSTREAM.md and
../../THIRD_PARTY_NOTICES.md.

## CPU-safe development policy

Do not run this worker in CPU-only CI. Use the root fake provider and adapter
unit tests for protocol validation. Those checks never import Torch, probe
CUDA, download the model or execute SAM3 inference.
