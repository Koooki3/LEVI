# LEVI · Robot Data Atelier

**LeRobot Exploration, Validation & Integration**

[![Checks](https://github.com/Koooki3/LEVI/actions/workflows/test.yml/badge.svg)](https://github.com/Koooki3/LEVI/actions/workflows/test.yml) [![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE) [![Version](https://img.shields.io/badge/LEVI-0.3.0-9bd654.svg)](CHANGELOG.md)

[简体中文](README.zh-CN.md)

**Guides** — [Conversion](docs/CONVERSION.md) · [RECAP export](docs/RECAP.md) · [Agent Workbench](docs/AGENT_WORKBENCH.md) · [Codex / Claude Pilot](docs/PILOT.md) · [SAM3 objects](docs/SAM3.md) · [Workspace layout](.state.md)
**Reference** — [Features](docs/FEATURES.md) · [API](docs/API.md) · [Agent Harness](docs/HARNESS.md) · [Validation record](docs/VALIDATION.md) · [Attribution](docs/UPSTREAM.md) · [Third-party notices](THIRD_PARTY_NOTICES.md)

LEVI is an independent robotics dataset browser, annotation editor, converter and review workbench derived from [LeRobot Dataset Visualizer](https://github.com/huggingface/lerobot-dataset-visualizer). English is the default; Chinese is available through the language switch. **The complete capture conversion pipeline is bundled** and requires no sibling repository or training environment: it inspects an input, reports which requirements it meets and which exports it supports, and writes LeRobot v2.1 or a RECAP (π\*0.6) value dataset. Raw robot captures can be browsed and annotated before conversion; their annotations carry over into the converted dataset.

The interface is designed for both standalone browsers and Hugging Face Space embeds. Language preference is kept per browser when storage is available, and the annotation workbench handles Ctrl/Cmd+S, Ctrl/Cmd+Z and playback keys without opening the browser's native Save Page dialog.

![LEVI interface](docs/assets/home-en.png)

Demo footage: the two public LeRobot datasets linked below. LEVI uses an original graphite, parchment and lime interface.

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
uv run levi stop              # stop the shared service (needed before clean)
uv run levi clean             # preview regenerable caches and orphaned artifacts
uv run levi clean --apply     # remove them, after confirming in the terminal
uv run levi migrate           # preview upgrading an older workspace's names/layout
uv run levi migrate --apply   # apply it (service stopped)
```

For a remote server, keep `ssh -L 7860:127.0.0.1:7860 USER@SERVER` running on your computer. The frontend defaults to loopback port **7860 (Web UI)** and proxies requests to **7861 (internal API)**. Forward local 7860 to server 7860, not server 7861. If you see `{"detail":"Not Found"}` or the API landing page, check the destination port. The API root now explains the distinction and links to the configured Web UI. `uv run levi backend` starts only the API. The launcher checks port conflicts and announces Ready only after both services respond.

## Agent-assisted review and annotation

Annotating a robot dataset is mostly looking: scrubbing video, deciding when an attempt began, whether it worked, and which object is which. LEVI lets an agent do the looking and hands you the judgement.

**An agent proposes; you approve and commit.** That boundary lives in the capability layer, not in a convention: no agent channel can approve a plan, accept a pilot, commit a changeset, reset or clean the workspace. Source datasets stay read-only. Every suggestion cites the exact frames it was read from, and the evidence ledger outlives the images themselves — a reviewer months later can still see what was looked at.

```bash
uv sync --locked --extra agent
uv run --extra agent levi build
uv run --extra agent levi
```

### Three ways to drive it

They differ in one thing that matters: who spends the tokens.

| Channel | Where the model runs | Who pays | Set up with |
| --- | --- | --- | --- |
| **Online** | inside LEVI, against an endpoint you configure | you, at that endpoint — LEVI meters it and records the cost itself | **Accounts & connections** → model profile |
| **External MCP** | in your own agent (Claude Code, Codex, any MCP client) | your agent's own context; it reports what it spent | `levi agent connect` |
| **Managed Pilot** | in a Codex or Claude Code session LEVI supervises | that session | [Pilot guide](docs/PILOT.md) |

The external MCP channel is the one most people use, so the walkthrough below follows it.

```text
        you                          LEVI                        your agent
         │                             │                              │
  ① plan ├───── episodes, cameras ────▶│                              │
         │      task type, definitions │                              │
  ② approve ───── freeze scope ───────▶│   (nothing starts yet)       │
         │                             │◀── runs.list ────────────────┤ ③ picks it up
         │                             │─── agent_prepare ───────────▶│
         │                             │◀── evidence.read (mosaic) ───┤
         │                             │◀── propose ──────────────────┤
  ④ watch ◀──── live activity ─────────┤                              │
  ⑤ review ──── accept / reject ──────▶│                              │
     commit ───────────────────────────▶ revision + artifact path
```

### A task, end to end

**1 — Plan it.** Open **Agent Workbench → Tasks & review**. The form offers what the dataset actually declares: click an episode or drag across a run of them, pick cameras from the list, and choose a task type — dataset review, video subtasks and events, or visible object masks. For subtask work you also write what a subtask *is*: when it starts, when it ends, and what counts as success. Those definitions are the contract the agent annotates against.

**2 — Approve it.** Approval freezes the scope: the episodes, the cameras, the instructions, the definitions and a digest of the source files. Nothing widens later. Approving does not publish anything, and on the MCP channel it does not start anything either — it unlocks the run.

**3 — Hand it over.** Tell your agent to pick up the latest run. It calls `runs.list`, which shows every run in its dataset scope and whose turn each one is:

```
human_approval → agent_prepare → agent_propose → human_review → human_commit
```

No run id to copy across. `workspace.get_context` gives a first-time agent the rest in one call: what it may do, what only you may do, the order to work in, and the habit that decides what the job costs.

**4 — Watch it.** **Live activity** streams every action as it happens — what the agent did, to which dataset and episode, how long it took, and the reason when something is refused. Each run also appears as a task card with its progress and what it is waiting for; clicking one filters the stream to that task.

**5 — Review and commit.** The queue shows each suggestion with the frames it cites. Accept, reject or edit; unknown outcomes are never quietly turned into labels. When you commit, the completion notice carries the revision's path and a link to the result.

### What makes the annotations trustworthy

- **Every claim cites frames.** A suggestion that references evidence outside its episode is refused by name, not with a generic error.
- **Uncertainty is first class.** "Sampled every 2 s, so the individual attempts cannot be separated" is a valid, recorded answer. Coverage gaps are reported rather than papered over.
- **The frozen snapshot is verified.** If the source moves under an approved plan, the run stops instead of annotating different data.
- **Nothing is published by an agent.** Committed revisions carry provenance and an inverse patch, so any commit can be undone through the same review path.

### Cost, measured rather than guessed

Evidence is what a task costs. `evidence.read` can return one labelled contact sheet per page instead of one image per frame, and `evidence.refine` adds frames only around boundaries the agent could not resolve. On one real run those two habits were the difference between roughly 43,000 and 130,000 tokens for ten episodes.

```bash
uv run levi agent usage show                            # per-agent history
uv run levi agent usage estimate --workflow temporal --episodes 20
```

`plans.estimate` prices a scope before you commit to it and states its basis, its sample count, and whether it is extrapolating beyond anything recorded. An online run needs no self-report — LEVI meters it and records the sample itself; an external agent reports its own, and the next estimate improves.

### Objects, with or without SAM3

`objects.strategy` reads this machine — worker, checkpoint, free GPU memory — and recommends a path. When SAM3 cannot run, `objects.detect` measures candidate regions with no model and no GPU, returning each one's outline, position, shape and median colour plus a labelled overlay; the agent names the ones that are objects and submits them by `candidate_id`. Both paths end in the same staged review.

Masks are annotated on sampled frames, so the playback bar marks the frames that carry one and steps between them; between those frames a track's last measured outline is carried forward, dashed and labelled with the frame it came from — visible continuity without claiming a position nobody measured.

### Where the work lands

Under `outputs/LEVI/workbench/agent/datasets/<dataset-name>/` relative to `LEVI_WORKSPACE`: one directory per dataset, and inside it runs and revisions named for the work and the minute — `temporal-20260920T0926`, `objects-20260920T0940`. Never a hash, never an opaque suffix.

```bash
uv run levi agent clean                    # free what runs.prepare can rebuild
uv run levi agent clean --abandon <run-id> # close a run nobody will finish
uv run levi agent reset --dataset <name>   # remove a dataset's agent history
```

`clean` frees input snapshots, evidence images and contact sheets of finished runs and keeps committed revisions, provenance, inverse patches, the evidence ledger and open drafts. `reset` is the deliberate counterpart: it removes the record of the work itself for one dataset, and never touches another dataset, your connections, or annotations no agent produced. Both preview first and ask before applying.

### Known limits

ACP-driven external sessions and HTTP MCP are deferred. Automated validation uses fixture and stub models: real-model annotation quality and SAM3 GPU inference are measured separately and are listed as open in the [validation record](docs/VALIDATION.md). Identity across frames cannot be recovered from outlines sampled seconds apart — LEVI says so rather than inventing tracks.

## SAM3 objects (optional)

SAM3 adds model-assisted object masks inside the annotation tab. It is optional: LEVI never downloads a checkpoint by itself, and the CPU checks never import Torch or probe CUDA. It needs an isolated Python 3.12 worker on a CUDA host, a Hugging Face account that can read `1038lab/sam3`, and about 7 GB of free GPU memory. When the GPU is busy or the worker is missing, an agent can outline the objects itself through the same review queue.

```bash
uv run levi sam3 check      # configuration only; no model, no CUDA
```

The complete first-deployment sequence, gates, prompts and review flow are in the [SAM3 guide](docs/SAM3.md).

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

### Default live demonstrations

Public LeRobot datasets, streamed rather than bundled:

- [lerobot/svla_so101_pickplace](https://huggingface.co/datasets/lerobot/svla_so101_pickplace) — SO-101 pick and place, 50 episodes, 11,939 frames, 30 fps, two 640×480 cameras.
- [lerobot/aloha_static_coffee](https://huggingface.co/datasets/lerobot/aloha_static_coffee) — bimanual ALOHA, 50 episodes, 55,000 frames, 50 fps, four 640×480 cameras.

Both are LeRobot v3.0. A demo that is later made private or removed upstream answers 401 to an anonymous request, which the viewer reports as such rather than as a LEVI permission error.

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

Before publishing, stop the service with `uv run levi stop`, preview `uv run levi clean`, then apply with `--apply`. It removes LEVI's regenerable caches and preserves datasets, annotations, review/job reports, `.env`, dependencies and production output. It also reports artifacts left behind by datasets that are no longer registered — empty directories are removed, anything holding review work is only listed, because a dataset leaving the catalog must not delete someone's annotations. Avoid `git clean -xfd` against a data workspace.

Repository: [Koooki3/LEVI](https://github.com/Koooki3/LEVI). Report reproducible problems and suggestions in [Issues](https://github.com/Koooki3/LEVI/issues). Read [CONTRIBUTING](CONTRIBUTING.md) before submitting changes and [RELEASING](docs/RELEASING.md) for the maintainer release procedure.

Preserve Apache-2.0 `LICENSE`, `NOTICE` and [attribution](docs/UPSTREAM.md). CI runs type/format checks, frontend tests, built-in conversion tests and a production build. See the [validation record](docs/VALIDATION.md) for tested scope and limitations.

If a conversion fails, inspect its structured report and logs. Duplicate/missing frame IDs, unknown gripper commands or video count mismatches stop processing before anything is published; the source is never modified. Interrupted jobs are marked on restart; a retry uses a new directory.


Harness execution approval, pilot gates, video evidence, extension contracts and current limits: [Agent Harness](docs/HARNESS.md).
