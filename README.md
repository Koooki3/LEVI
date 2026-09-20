# LEVI · Robot Data Atelier

See [Codex / Claude Code Pilot](docs/PILOT.md) for headless MCP, managed sessions, human terminal approval and live tracking.

**LeRobot Exploration, Validation & Integration**

[![Checks](https://github.com/Koooki3/LEVI/actions/workflows/test.yml/badge.svg)](https://github.com/Koooki3/LEVI/actions/workflows/test.yml) [![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE) [![Version](https://img.shields.io/badge/LEVI-0.3.0-9bd654.svg)](CHANGELOG.md)

[简体中文](README.zh-CN.md) · [Conversion guide](docs/CONVERSION.md) · [RECAP export](docs/RECAP.md) · [Workspace layout](.state.md) · [Features](docs/FEATURES.md) · [API](docs/API.md) · [Validation](docs/VALIDATION.md) · [Attribution](docs/UPSTREAM.md) · [Third-party notices](THIRD_PARTY_NOTICES.md) · [SAM3 object annotation](docs/SAM3.md) · [Agent Workbench](docs/AGENT_WORKBENCH.md)

LEVI is an independent robotics dataset browser, annotation editor, converter and review workbench derived from [LeRobot Dataset Visualizer](https://github.com/huggingface/lerobot-dataset-visualizer). English is the default; Chinese is available through the language switch. **The complete capture conversion pipeline is bundled** and requires no sibling repository or training environment: it inspects an input, reports which requirements it meets and which exports it supports, and writes LeRobot v2.1 or a RECAP (π\*0.6) value dataset. Raw robot captures can be browsed and annotated before conversion; their annotations carry over into the converted dataset.

The interface is designed for both standalone browsers and Hugging Face Space embeds. Language preference is kept per browser when storage is available, and the annotation workbench handles Ctrl/Cmd+S, Ctrl/Cmd+Z and playback keys without opening the browser's native Save Page dialog.

![LEVI interface](docs/assets/home-en.png)

Demo footage: the two `samanthalhy` datasets linked below, whose cards declare Apache-2.0. LEVI uses an original graphite, parchment and lime interface.

## Install and run

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and `ffmpeg`/`ffprobe`. From your cloned repository, whose name and location are unrestricted:

```bash
git clone https://github.com/Koooki3/LEVI.git
cd LEVI
export LEVI_WORKSPACE="$PWD/.state"
cp .env.example .env
export UV_CACHE_DIR="$LEVI_WORKSPACE/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$PWD/.runtime/python"
uv sync --locked
uv run levi setup
uv run levi build
uv run levi serve
```

`uv run levi` is equivalent to `uv run levi serve` and starts both services. Wait for **Ready**, then open **http://127.0.0.1:7860**. Ctrl+C stops both services. Setup installs checksum-verified Bun 1.3.10 in `.runtime/` and locked frontend dependencies. Python dependencies, including the converter, live in `.venv/` and `uv.lock`.

Linux x86_64 is tested. The installer also selects Linux/macOS ARM64 and macOS x86_64 binaries; use WSL2 for Windows. Install FFmpeg through your OS package manager, e.g. `sudo apt install ffmpeg` or `brew install ffmpeg`.

```bash
uv run levi dev
uv run levi serve --port 7870 --backend-port 7871
uv run levi convert --help
uv run levi clean             # preview regenerable caches
uv run levi clean --apply     # remove the listed caches
uv run levi migrate           # preview upgrading an older workspace's names/layout
uv run levi migrate --apply   # apply it (service stopped)
```

For a remote server, keep `ssh -L 7860:127.0.0.1:7860 USER@SERVER` running on your computer. The frontend defaults to loopback port **7860 (Web UI)** and proxies requests to **7861 (internal API)**. Forward local 7860 to server 7860, not server 7861. If you see `{"detail":"Not Found"}` or the API landing page, check the destination port. The API root now explains the distinction and links to the configured Web UI. `uv run levi backend` starts only the API. The launcher checks port conflicts and announces Ready only after both services respond.

## Agent-assisted review and annotation (experimental)

The **Agent Workbench** adds evidence-grounded drafts and a focused human review queue. SAM3 is an optional object tool within this workflow; source datasets remain read-only. [Full guide, MCP setup, architecture and limitations](docs/AGENT_WORKBENCH.md).

After the normal installation, run in order:

```bash
export LEVI_WORKSPACE="$PWD/.state"
uv sync --locked --extra agent
uv run --extra agent levi build
uv run --extra agent levi
```

1. **Accounts & connections** → create a compatible model profile and declare its image capability. Use a server environment-variable key or a server-memory session key. Profiles show connection state and support select/edit/disconnect/remove; HF identity has its own switch/sign-out menu.
2. Choose a dataset, explicit episodes/cameras, instructions and budget. Approve media egress only for the chosen endpoint, then **Inspect & create plan**.
3. **Run pilot**, review its evidence, then **Execute remaining**. Completed shards and human edits survive resumption.
4. For object masks, open **SAM3 · optional object tool**: plan a bounded scope, explicitly run the configured worker, review overlays and accept/reject tracks. The Agent tool needs an existing checkpoint and does not download one.
5. Filter pending suggestions or issues; use J/K, accept/reject one or a batch, and edit text/time bounds. Save draft edits before recording decisions. Evidence following preserves the current editor.
6. **Validate & approve** → **Commit approved changes**. Unknown outcomes are not converted into human labels; rejected suggestions are excluded. Version conflicts require a fresh review. **Create undo draft** also requires approval.
7. Resume interrupted tasks after restoring credentials, or review the completed subset. Export from the existing dataset controls; native export includes annotations/provenance, while raw-capture conversion carries them using source-frame mappings.

Artifacts live under `outputs/LEVI/workbench/agent/datasets/<dataset-name>/` relative to `LEVI_WORKSPACE`, with readable timestamp runs/revisions. No hash suffixes are used for LEVI artifact names. External MCP Agents can prepare evidence and draft suggestions, but cannot approve or commit. ACP-driven external sessions and HTTP MCP are deferred. Automated validation uses fixture models only; real-model quality and SAM3 GPU inference remain separate manual checks.

## SAM3 first-deployment sequence

Run these commands from the cloned LEVI root. The checkout name and workspace path are unrestricted:

~~~bash
# 1) Select a workspace and install the core
export LEVI_WORKSPACE="$PWD/.state"
cp .env.example .env
uv sync --locked
uv run levi setup

# 2) Sign in to an account that can read 1038lab/sam3
HF_HOME="$LEVI_WORKSPACE/.cache/huggingface" hf auth login
HF_HOME="$LEVI_WORKSPACE/.cache/huggingface" hf auth whoami
# Without a global hf command:
# HF_HOME="$LEVI_WORKSPACE/.cache/huggingface" uvx hf auth login
# HF_HOME="$LEVI_WORKSPACE/.cache/huggingface" uvx hf auth whoami

# 3) Install the isolated worker on the CUDA host
uv venv --python 3.12 integrations/sam3/.venv
uv sync --project integrations/sam3
export LEVI_SAM3_WORKER_PYTHON="$PWD/integrations/sam3/.venv/bin/python"
export LEVI_SAM3_ENABLED=1

# 4) Run model-free configuration checks
uv run --project integrations/sam3 levi-sam3-worker --check
uv run levi sam3 check

# 5) Build and start the web workbench
uv run levi build
uv run levi serve
~~~

After Ready appears, open http://127.0.0.1:7860. On any dataset annotation page, complete the Hub access, CUDA worker and checkpoint gates. After sign-in, a missing checkpoint shows an explicit download prompt, workspace path, progress bar and Download checkpoint button; click it and wait for Checkpoint ready before choosing prompts, scope and cameras and starting annotation. The download uses the active Hugging Face session, saves sam3.pt to $LEVI_WORKSPACE/checkpoints/sam3 and can be retried after an error; later datasets reuse the workspace copy. LEVI validates the model-neutral plan before starting the worker. Changing the Hugging Face account or workspace creates separate Hub snapshots and sidecars using account and workspace scopes. Set LEVI_SAM3_ENABLED=0 only when you want to hide the real worker. Never commit tokens, checkpoints or workspace data.

## Portable workspace

`LEVI_WORKSPACE` is the data workspace, separate from the Git checkout if desired. It defaults to `.state/` inside the repository. Relative values resolve against the startup directory. Set it in your shell or untracked `.env` (see `.env.example`). No machine-specific parent marker or unrelated project variable is consulted.

| Item | Location |
| --- | --- |
| Code, lockfiles | Git checkout |
| Python, Bun, frontend dependencies | Checkout `.venv/`, `.runtime/`, `node_modules/` |
| Catalog, annotations, outcome labels, reviews, jobs, reports | Workspace `outputs/LEVI/workbench/` |
| Browsing views of raw captures | Workspace `outputs/LEVI/workbench/views/<name>/` |
| Converted datasets (default) | Workspace `<source>_<lerobot\|recap>_<timestamp>/` — the directory is the dataset |
| Annotation exports | Workspace `outputs/LEVI/exports/<name>_annotated/` |
| Downloads/runtime caches | Workspace `.cache/` and `tmp/` |

Place captures under the workspace, or point the workspace to their common parent. Local registration and conversion enforce the resolved path boundary. Conversion rejects symlink inputs and existing output directories.

LEVI keeps the dataset list in step with the workspace while it runs: datasets copied in are registered, episodes/demos added or removed are picked up (raw captures get their view rebuilt, annotations re-keyed by demo), deleted datasets drop out, and an open viewer offers a reload when its dataset changed. Tune or disable it with `LEVI_SYNC_INTERVAL`, `LEVI_SYNC_SETTLE` and `LEVI_SYNC_DISCOVER` (see `.env.example` and the [workspace reference](.state.md#runtime-sync)).

Names never carry hash suffixes: per-dataset artifacts (annotations, reviews, diagnostics, exports) use the dataset's catalog name, per-run artifacts (jobs, conversion outputs, SAM3 revisions) a timestamp. To upgrade a workspace from an older LEVI, set it in `.env`, stop the service and run `uv run levi migrate` (dry run) then `--apply`; old `/local/<hash>` links keep working. See the [workspace reference](.state.md).

## Features

- Hub search, pagination, public/private access and local dataset registration.
- Synchronized multi-camera playback, timeline, shortcuts, fullscreen, visibility controls and state/action charts; multi-task datasets can filter the episode list by task.
- Language timelines: persistent task augmentation/subtask/plan/memory; speech/interjection/VQA; SAM3 object/track sidecars; bounding boxes and points on video, count/attribute/spatial answers.
- Statistics and episode lengths; movement/smoothness/length filtering; first/last camera frames and per-dataset review flags.
- Action autocorrelation/chunk suggestions, state-action alignment, demonstrator speed and cross-episode variance, scoped to the full dataset, an episode range or a single task, with an optional full-coverage (unsampled) pass.
- Upstream-supported 3D robot playback, joint mapping and end-effector trails.
- Native Doctor with optional sampled video checks and JSON reports; external original Doctor link.
- Built-in conversion: input inspection with a per-requirement checklist, per-target compatibility with reasons and one-click fixes, LeRobot v2.1 and RECAP value exports, single-pass parallel conversion with lossless retime, live progress, immutable plans and automatic registration.
- Raw captures register as datasets: a lossless browsing view makes viewing, statistics, annotation, SAM3 and outcome labels available before conversion; annotations carry over by source demo.
- Per-episode success/failure labels (click the sidebar dot); the dataset list describes each entry's format, version and origin (raw capture, LEVI conversion, RECAP, annotated export, external).

Video-based LeRobot v2.0/v2.1/v3.0/v3.1 can be browsed. Embedded-image Parquet playback retains the upstream limitation. Original dataset text and feature/joint identifiers remain unchanged.

### SAM3 object annotation (global)

The annotation tab provides the same SAM3 object/track sidecar for built-in demos, Hub datasets and registered local datasets. The UI checks Hub access, the CUDA worker and the checkpoint first, then builds and validates a plan across an episode range, task selection or the full dataset and selected cameras. Each episode/camera pair is processed independently; model suggestions are stored as lossless RLE sidecar data and native LeRobot files stay read-only. The page shows the current Hugging Face account, worker state, checkpoint download progress and workspace path, and exposes track-level frame ranges with accept/reject review. The default model is sam3.pt from 1038lab/sam3. Real jobs need the isolated Python 3.12 uv worker on a CUDA host; core CPU checks never import Torch, probe CUDA, download a model or run inference. See the SAM3 guide for the complete sequence.

Default live demonstrations:

- [samanthalhy/so100_strawberry_2](https://huggingface.co/datasets/samanthalhy/so100_strawberry_2)
- [samanthalhy/eval_so100_smol_strawberry_2](https://huggingface.co/datasets/samanthalhy/eval_so100_smol_strawberry_2)

The evaluation collection has 10 episodes, 32,033 frames, 30 FPS and three 640×480 cameras. Videos are streamed, not bundled.

## Built-in conversion

In **Conversion & review**: enter a raw capture folder (`task/demo_NNNN` with pose/gripper CSVs and one video or image folder per camera) or a LeRobot v2.x dataset and click **Inspect input**. LEVI detects the format (teleoperation or policy-rollout capture, image sequence, LeRobot), lists every requirement with its status — decode-only checks are marked "checked while converting", never shown as passed — and rates each export:

| Export | For | Default timing |
| --- | --- | --- |
| LeRobot v2.1 | imitation learning, openpi, the LEVI viewer | `resample` to the target FPS (lowered to the measured rate), static frames filtered |
| RECAP value dataset (π\*0.6) | training a RECAP value function; RLinf-compatible `meta/returns.parquet`, `is_success`, per-step rewards | `retime`: every captured frame kept, videos stream-copied losslessly |

An export can be unsupported (for example RECAP on teleoperation data without success/failure labels); the card says why and offers fixes — label outcomes in LEVI, exclude the affected episodes, or export as demonstrations. Choose an export, adjust options, review the plan and run it; the job shows stages, progress, current episode and time remaining. Output is written to a hidden staging directory and published only if every episode passed the preflight and the dataset validated.

```bash
uv run levi convert inspect  --source captures/session-a --output unused
uv run levi convert pipeline --source captures/session-a --output session-a-lerobot
uv run levi convert pipeline --source captures/session-a --output session-a-recap \
  --options configs/recap.json   # {"target": "recap_value"}
```

Paths are relative to `LEVI_WORKSPACE` or absolute within it. One pass per camera: the source is decoded once while the preflight scans it and the single H.264 encode runs; in `retime` mode videos are remuxed with exact `i / fps` timestamps and bit-identical pixels. Work runs in parallel worker processes. See the [conversion guide](docs/CONVERSION.md) for schemas, the formats table, options, timing modes and provenance, and [RECAP.md](docs/RECAP.md) for the RECAP format, its references (paper, RLinf, LeRobot proposal) and how to consume it.

Default state is absolute XYZ in metres, continuous Euler rotation in radians and binary gripper command (open=1, close=0). Quaternion rotation is optional. Default action is the next state, with the final state repeated. Gripper command is not measured aperture; these semantics must match your policy.

The viewer's Doctor samples video frames. Conversion validation checks every video's frame count, rate and resolution against the parquet and metadata. Neither promises compatibility with every training framework.

### Raw captures: browse, annotate, convert

Register a raw capture folder under **Local datasets** like any dataset. LEVI builds a browsing view in the background (seconds: videos are only remuxed) and the capture opens in the viewer with every frame. Viewing, statistics, filtering, frame gallery, Action Insights, language/event annotation, SAM3 objects, outcome labels and review flags work; Doctor checks the view; exporting asks you to convert first. When the capture is converted, its annotations, outcome labels and SAM3 masks move to the matching frames of the new dataset (report in `meta/levi_annotation_carryover.json`).

## Save, export and review

Saving an episode writes a workspace annotation sidecar without changing source files. Offline edits remain in browser session storage until the backend is restored and saved. Dataset export saves current edits first, then writes language columns into a new dataset, preserving its container version; human outcome labels are written as `levi_outcome` in `meta/episodes.jsonl`. A raw capture's browsing view cannot be exported — convert it instead.

Annotation export uses video hardlinks where possible; use API `copy_videos=true` for independent copies. Built-in conversion outputs use independent files. Review manifests contain dataset IDs, excluded episode IDs and notes; converted `meta/levi_provenance.jsonl` maps episodes to capture paths. Review that mapping before configuring `exclude_demos`. Flags never delete source data.

Hub upload is an explicit API action requiring a target repository and token; normal conversion and saving do not upload. See [API](docs/API.md).

## Accounts and deployment

Token sign-in validates with HF, stores the token in browser local storage and a video-proxy HttpOnly cookie, and clears both on sign-out. OAuth and backend `HF_TOKEN` are also supported. Never commit credentials or `.env`. Use `LEVI_SECURE_COOKIES=1` for HTTPS/Space iframe deployments.

This is a single-user local workbench with file access. HF sign-in controls Hub access, not LEVI user permissions. Put shared deployments behind an authenticated reverse proxy.

```bash
docker build -t levi:local .
docker run --rm -p 127.0.0.1:7860:7860 \
  -v "$HOME/levi-data:/workspace" levi:local
```

The container includes the converter, Python environment and FFmpeg. No additional code repository is mounted.

## Develop and publish

```bash
# Keep the workspace/cache variables from installation
uv sync --locked --group dev
uv run levi check
mkdir -p "$LEVI_WORKSPACE/tmp/build"
uv run pytest --basetemp="$LEVI_WORKSPACE/tmp/build/pytest"
uv run levi build
# Optional: start the service, then run live checks
PLAYWRIGHT_BROWSERS_PATH="$LEVI_WORKSPACE/.cache/playwright" uv run playwright install chromium
uv run python scripts/verify_browser.py
uv run python scripts/verify_conversion.py
```

Before publishing, preview `uv run levi clean`, then apply with `--apply`. It removes LEVI's regenerable caches and preserves datasets, annotations, review/job reports, `.env`, dependencies and production output. Avoid `git clean -xfd` against a data workspace.

Repository: [Koooki3/LEVI](https://github.com/Koooki3/LEVI). Report reproducible problems and suggestions in [Issues](https://github.com/Koooki3/LEVI/issues). Read [CONTRIBUTING](CONTRIBUTING.md) before submitting changes and [RELEASING](docs/RELEASING.md) for the maintainer release procedure.

Preserve Apache-2.0 `LICENSE`, `NOTICE` and [attribution](docs/UPSTREAM.md). CI runs type/format checks, frontend tests, built-in conversion tests and a production build. See the [validation record](docs/VALIDATION.md) for tested scope and limitations.

If a conversion fails, inspect its structured report and logs. Duplicate/missing frame IDs, unknown gripper commands or video count mismatches stop processing before anything is published; the source is never modified. Interrupted jobs are marked on restart; a retry uses a new directory.


Harness execution approval, pilot gates, video evidence, extension contracts and current limits: [Agent Harness](docs/HARNESS.md).
