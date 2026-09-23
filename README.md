# LEVI · Robot Data Atelier

**LeRobot Exploration, Validation & Integration**

[![Checks](https://github.com/Koooki3/LEVI/actions/workflows/test.yml/badge.svg)](https://github.com/Koooki3/LEVI/actions/workflows/test.yml) [![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE) [![Version](https://img.shields.io/badge/LEVI-0.3.0-9bd654.svg)](CHANGELOG.md)

English · [简体中文](README.zh-CN.md)

LEVI is a local workbench for robot-learning data. It browses LeRobot datasets and raw robot captures, converts them into training formats, checks their quality, and annotates them with AI agents whose every suggestion a person reviews before it is published. It runs on your machine: source data is never modified, and nothing leaves the machine unless you connect a remote model.

![LEVI interface](docs/assets/home-en.png)

## What it does

| | |
| --- | --- |
| **Browse and analyze** | Synchronized multi-camera playback, state/action charts, statistics, filtering, frame gallery, Action Insights and 3D robot replay for LeRobot v2.0–v3.1, local or on the Hugging Face Hub. Derived from the [LeRobot Dataset Visualizer](https://github.com/huggingface/lerobot-dataset-visualizer). |
| **Convert** | Raw captures (CSV + video or image folders) and LeRobot v2.x into **LeRobot v2.1** or a **RECAP (π\*0.6) value dataset**, after an inspection that lists which requirements the input meets. Single-pass, parallel, with a lossless retime mode. Raw captures can be browsed and annotated before conversion; the annotations carry over. |
| **Check and curate** | Structural quality checks over every episode (timestamps, actions, video, metadata), episode outcome labels, review flags, and agent-driven content review — which task a demo really shows, whether it succeeded. |
| **Annotate with agents** | Subtask segments, events and object masks proposed by an external MCP agent (Claude Code, Codex, …), an API model or a **local model on Ollama**, then reviewed, committed and undoable by a person. A natural-language request can start a whole task. |
| **Learn from every task** | The harness closes each task with a ledger, measured token and time cost, a local memory of what was verified on that dataset, and improvement candidates that a person can publish for the next task. |

## Quick start

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) and FFmpeg (`sudo apt install ffmpeg` or `brew install ffmpeg`). Linux x86_64 is tested; macOS works; use WSL2 on Windows.

```bash
git clone https://github.com/Koooki3/LEVI.git
cd LEVI
export LEVI_WORKSPACE="$PWD/.state"          # where datasets and LEVI's state live
export UV_CACHE_DIR="$LEVI_WORKSPACE/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$PWD/.runtime/python"
cp .env.example .env
uv sync --locked --extra agent
uv run levi setup                             # checksum-verified Bun + frontend dependencies
uv run levi build
uv run levi                                   # starts the Web UI and the API
```

Wait for **Ready** and open **http://127.0.0.1:7860**. The home page streams two public LeRobot datasets ([svla_so101_pickplace](https://huggingface.co/datasets/lerobot/svla_so101_pickplace), [aloha_static_coffee](https://huggingface.co/datasets/lerobot/aloha_static_coffee)) as demonstrations. Put your own datasets or raw captures under `$LEVI_WORKSPACE`; they are registered automatically. Ctrl+C stops both services.

On a remote server, forward the Web UI port: `ssh -L 7860:127.0.0.1:7860 user@server`. Port 7861 is the internal API, not the workbench.

For a strict CPU-only LEVI session, set `export LEVI_CPU_ONLY=1` before starting the service. This disables its background GPU watcher and blocks local accelerator-backed inference and SAM3.

DROID raw folders (`demo_0000/trajectory.h5`, metadata and three MP4 cameras) are an optional **browse-and-annotate input**, not a supported training conversion. For that input, install `uv sync --locked --extra agent --extra droid`, place the dataset folder directly under `$LEVI_WORKSPACE`, and use **Sync now** or restart LEVI. A read-only derived view appears at `outputs/LEVI/workbench/views/<dataset-name>/`; the HDF5 source is untouched. The view uses a nominal 14.3 FPS clock because source MP4 time and control time differ. Original timestamps and per-episode drift are saved in `meta/levi_provenance.jsonl`; review precise temporal boundaries against them. See [Conversion](docs/CONVERSION.md#droid-raw-browsing-view). A new workspace also downloads its own DROID test dataset, 500 episodes of the public release drawn reproducibly (`uv run levi sample draw` makes another 500), — see [DROID test sample](docs/WORKSPACE.md#droid-test-sample).

```bash
uv run levi stop                # stop the shared service and its workers (--all: also LEVI's Ollama)
uv run levi clean               # preview regenerable caches (service stopped); --apply to remove
uv run levi migrate             # preview upgrading an older workspace; --apply to apply
uv run levi convert --help
uv run levi agent --help        # tasks, connections, reviews, memory, improvements from a terminal
```

## Agents, with a person in charge

An agent reads sampled evidence and proposes; only a person approves a plan, accepts a pilot and commits. The boundary is enforced in one capability layer shared by the web UI, REST, MCP and the CLI. Every suggestion cites the frames it was read from, uncertainty is a valid answer, and a commit can be undone through the same review.

| Channel | Model | Set up with |
| --- | --- | --- |
| External MCP | Your own agent (Claude Code, Codex, any MCP client) | `uv run levi agent connect --client claude --project <dir> --dataset local/<name> --apply` |
| Online | An OpenAI-compatible endpoint, metered by LEVI | Agent Workbench → Accounts & connections |
| Local | An Ollama model on this machine (default `qwen3.5:4b`), metered, no API key | [Local models](docs/OLLAMA.md) |
| Managed Pilot | A Codex or Claude Code session LEVI supervises | [Pilot](docs/PILOT.md) |

A task goes **plan → approve → pilot → review pilot → remaining episodes → review → commit**. With a local model, one sentence can start it:

```bash
uv run levi agent task new "Check the quality of <dataset>, then annotate subtasks on the first 10 demos and report tokens and time" --provider qwen-local
```

LEVI checks the spec against the catalog and waits for your approval before anything runs. Around every task, the **harness** records what it cost (tokens and time, per agent and per dataset), keeps a memory of human-verified facts that the next task starts from, and files improvement candidates that you can evaluate and publish. On a GPU shared with robot training, the local model runs off-peak and yields automatically. See [Agents](docs/AGENTS.md).

## Documentation

| Guide | Contents |
| --- | --- |
| [Agents](docs/AGENTS.md) | Channels, task lifecycle, natural-language tasks, evidence, object masks, the harness (cost, memory, self-improvement, teacher supervision), capability reference |
| [Local models](docs/OLLAMA.md) · [中文](docs/OLLAMA.zh-CN.md) | Ollama setup, model binding, off-peak GPU use, the teacher/learner loop |
| [Pilot](docs/PILOT.md) · [中文](docs/PILOT.zh-CN.md) | Managed Codex / Claude Code sessions |
| [Data quality](docs/QUALITY.md) | Structural checks, content review, labels, flags and review manifests |
| [Conversion](docs/CONVERSION.md) | Input formats, inspection, timing modes, options, performance, provenance |
| [RECAP](docs/RECAP.md) | The RECAP value-dataset format and how to consume it |
| [SAM3](docs/SAM3.md) | Optional model-assisted object masks |
| [Evaluation records](docs/EVALUATION.md) | Recording human annotation work; per-mode quality and cost records of human and agent subtask annotation |
| [Built-in knowledge](docs/KNOWLEDGE.md) | Dataset-agnostic rules every model in LEVI follows, and how local memory is promoted into them |
| [Workspace](docs/WORKSPACE.md) | What lives where under `LEVI_WORKSPACE`, naming, sync, cleanup |
| [API](docs/API.md) | REST routes and agent capabilities |
| [Validation](docs/VALIDATION.md) | What has been tested, on what, and what has not |
| [Architecture status](docs/architecture/IMPLEMENTATION_STATUS.md) | Implementation progress against the architecture plan |
| [Upstream](docs/UPSTREAM.md) · [Releasing](docs/RELEASING.md) · [Changelog](CHANGELOG.md) | Attribution and feature parity, release procedure, changes |

## Workspace

`LEVI_WORKSPACE` holds data and state and can live outside the checkout (default `.state/`). Datasets sit directly under it; LEVI's own state is under `outputs/LEVI/`; model weights under `checkpoints/`. Names follow the dataset and the episode (`episode_000007`), runs a readable timestamp (`temporal-20260922T0941`) — never a hash. LEVI follows the workspace while it runs: datasets copied in are registered, changed ones refreshed, removed ones dropped. See [Workspace](docs/WORKSPACE.md).

## Deployment

LEVI is a single-user local workbench with file access. Hugging Face sign-in (a token, OAuth, or `HF_TOKEN` on the backend) controls Hub access, not LEVI permissions; a browser token is kept in local storage and an HttpOnly video-proxy cookie and cleared on sign-out. Never commit credentials or `.env`; put any shared deployment behind an authenticated reverse proxy and set `LEVI_SECURE_COOKIES=1` for HTTPS or Space embeds. Hub upload is an explicit API action; conversion and saving never upload.

```bash
docker build -t levi:local .
docker run --rm -p 127.0.0.1:7860:7860 -v "$HOME/levi-data:/workspace" levi:local
```

## Status and limits

The current release is **0.3.0**; `main` carries the unreleased agent harness, local-model and data-curation work listed in the [changelog](CHANGELOG.md). Evidence is sampled: dense refinement looks where it is pointed and cannot prove that nothing happened elsewhere. Model annotation quality is measured per dataset rather than claimed; automated tests use fixtures and stub models, and the local model's annotation quality is still being established. MCP is stdio only. There is no multi-user access control, OS-level offline sandbox or GPU scheduler. Details: [Validation](docs/VALIDATION.md).

## Troubleshooting

- **`{"detail":"Not Found"}` or the API landing page** — you reached port 7861; open 7860 and check your SSH or editor port forwarding targets server port 7860.
- **A local path is refused** — it must be a LeRobot dataset (`meta/info.json`) or a recognised raw capture (`task/demo_NNNN` or a DROID `demo_NNNN/trajectory.h5`), and its real path must be inside `LEVI_WORKSPACE`.
- **A conversion fails** — read the inspection checklist and the job log. Duplicate or missing frame ids, unknown gripper commands or video count mismatches stop it before anything is published; the source is never modified.
- **A local-model run is blocked** — the reason names the process using the GPU; the run resumes once the GPU has been free for `LEVI_GPU_QUIET_SECONDS`.
- **Interrupted after a restart** — jobs are marked interrupted and never resume writing on their own; plan again.

## Development

```bash
uv sync --locked --group dev --extra agent
uv run levi check
mkdir -p "$LEVI_WORKSPACE/tmp/build"
uv run pytest --basetemp="$LEVI_WORKSPACE/tmp/build/pytest"
export PATH="$(ls -d "$PWD"/.runtime/bun-*):$PATH"   # the Bun that `levi setup` installed
bun run format && bun run validate                # type check, lint, format, frontend tests
uv run levi build
```

CI runs the same checks and a production build. Read [CONTRIBUTING](CONTRIBUTING.md) before submitting changes; report problems in [Issues](https://github.com/Koooki3/LEVI/issues). LEVI is licensed under Apache-2.0 and keeps the upstream `LICENSE`, `NOTICE` and [attribution](docs/UPSTREAM.md); see [third-party notices](THIRD_PARTY_NOTICES.md).
