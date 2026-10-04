# Installing LEVI

From an empty machine to a running LEVI, then the optional parts one by one. [中文](INSTALL.zh-CN.md) · For an AI agent doing the installation: [INSTALL.agent.md](INSTALL.agent.md).

Two commands do the work and say what is left:

- `uv run levi install --profile <profile> [--plan] [--json] [--yes]` sets up a profile. It is idempotent (a step already done is skipped), does what it can by itself, and lists what a person must do. `--plan` only lists the steps (network use, download size, root, token, consent) and changes nothing.
- `uv run levi doctor [--json]` checks the machine, read only: Python and uv, ffmpeg, Bun and the frontend dependencies, whether the production build matches the sources, ports 7860/7861, the workspace (writable, path short enough for the core's socket, free disk), the training-pool settings (set or not; values are never shown), the optional workers and their weights, a Hugging Face token (present or not), the GPU and driver, local model servers (loopback `GET` only), and the live service's configuration.

Exit codes of both: **0** done / all well, **1** warnings (doctor), **2** a failure that a command fixes (the report names it), **10** what is left needs a person (root, a token, a licence, a choice) or `--yes` for a large download. `levi install --plan` exits 0 when nothing is left to do, 1 when only automatic steps are left, 10 when a person's steps are left.

## 1. Before you start

| | |
| --- | --- |
| Platform | Linux x86_64 is the tested platform. Process tracking, the GPU guard and the live service read `/proc`; macOS and WSL2 are not verified |
| Tools | `git`, [uv](https://docs.astral.sh/uv/getting-started/installation/), `ffmpeg` with `ffprobe` (`sudo apt install ffmpeg`, or static binaries on `PATH` without root). Node.js is not needed (LEVI installs a pinned, checksum-verified Bun), except for the Managed Pilot (Node.js 22+) |
| Disk | about 2 GiB for LEVI and its dependencies, plus your data; optional workers take 5-8 GiB each, a local model 3-30 GiB |
| GPU | optional. An NVIDIA driver for local models, SAM3, fast segmentation and RECAP value models. `LEVI_CPU_ONLY=1` switches every GPU feature off |

## 2. The core

```bash
git clone https://github.com/Koooki3/LEVI.git && cd LEVI
export LEVI_WORKSPACE="$PWD/.state"      # where datasets and LEVI's state live (any folder; keep the path short)
uv run --locked levi install --profile core --plan    # what will happen
uv run --locked levi install --profile core           # Python deps, .env, workspace, Bun + frontend deps, build
uv run levi doctor
uv run levi                                           # starts the web UI (7860) and the API (7861)
```

`uv run` creates the Python environment on first use. The core profile then runs `uv sync --locked --inexact` (it keeps packages already there, such as a developer's dev group), copies `.env.example` to `.env` when there is no `.env` (an existing one is never touched), creates the workspace, runs `levi setup` (Bun and `bun install --frozen-lockfile`) and `levi build`. Open http://127.0.0.1:7860. `levi build` itself runs `bun install --frozen-lockfile` (with LEVI's Bun) before building when `node_modules` does not match `bun.lock`/`package.json`: it is missing, the content of either file differs from the one recorded at the last install (`node_modules/.levi-deps-installed` holds their sha256; without that record it installs once), or a dependency `package.json` names is not installed; a build that still fails says to run `uv run levi setup`. While a LEVI of this checkout runs (the product LEVI, or a live service started from it: both `next start` read this checkout's `node_modules` and `.next`), `levi build` refuses (exit 3) and names what to stop (`uv run --no-sync levi stop`, `uv run --no-sync levi live stop`); `levi build --while-serving` builds anyway, with a warning and without installing dependencies, and the running page may break until it is restarted.

The workspace path matters: the core listens on a Unix socket inside it, and the socket path must stay within 103 bytes, so `levi doctor` fails a path that is too long. A new workspace downloads nothing by itself; `uv run levi sample fetch droid` fetches a 500-episode DROID test sample (about 11.6 GiB) when you want one ([Workspace](docs/WORKSPACE.md#droid-test-sample)).

Settings live in `.env` (every variable LEVI reads is listed in `.env.example`, with what it does). The ones a setup usually needs:

| Variable | When |
| --- | --- |
| `LEVI_WORKSPACE` | always, unless the checkout's `.state` suits you |
| `LEVI_POOL_ROOTS`, `LEVI_POOL_HELDOUT`, `LEVI_EXPORT_ROOTS` | the training pool: read-only folders to index, the held-out lists (exports are refused while unset; `none` says there are none: a person decides), where exports may go ([Training pool](docs/TRAINING_POOL.md)) |
| `LEVI_GPU_LOCK_FILE` | the GPU is shared with other tools that take the same `flock` |
| `HF_TOKEN` | private or gated Hugging Face repositories, SAM3 weights, uploads (or sign in from the web UI) |
| `LEVI_CPU_ONLY=1` | a machine without a GPU, or one LEVI must not use |

## 3. Optional parts

`uv run levi install --profile <name>` for each (several `--profile` at once are fine; `core` is always included). Anything larger than 1 GiB downloads only with `--yes`.

| Profile | LEVI does | A person does | Docs |
| --- | --- | --- | --- |
| `agent` | `uv sync` with the `agent` extra (MCP, model runs) | `levi agent connect --client claude\|codex --project <dir> --dataset local/<name> --apply`: which datasets an agent may see is a person's choice | [Agents](docs/AGENTS.md) |
| `local-model` | the `agent` extra | install Ollama (or build a vLLM environment), choose and download a model, create and bind the model profile in the web UI | [Local models](docs/OLLAMA.md), [vLLM](docs/VLLM.md) |
| `segmentation` | `integrations/segmentation/setup.sh` (Python 3.12, Torch CUDA, about 5 GiB) | — (the base weights download on first use, no token) | [Fast segmentation](docs/SEGMENTATION.md) |
| `sam3` | the SAM3 worker environment (`integrations/sam3`, about 5 GiB) | sign in to Hugging Face, accept the SAM licence, download the weights in the web UI | [SAM3](docs/SAM3.md) |
| `recap` | `integrations/recap_value/setup.sh` (about 7 GiB) | import a RECAP value checkpoint (none ships with LEVI) | [RECAP](docs/RECAP.md) |
| `live` | the `agent` extra | build the vLLM environment and download the weights, write a `live.toml` that names this machine's rollout folder | [Live](docs/LIVE.md), [vLLM](docs/VLLM.md) |
| `pilot` | `bun install` in `integrations/pilot` | Node.js 22+ and a signed-in `codex` or `claude` CLI | [Pilot](docs/PILOT.md) |

The worker environments use Torch's CUDA 12.8 wheels; a machine whose driver is older than that, or one without an NVIDIA GPU, cannot run them as shipped.

## 4. Running it as a service

`levi serve` runs in the foreground. For a machine that keeps it running, a systemd **user** unit (a template, not tested by the maintainers; adjust the paths):

```ini
# ~/.config/systemd/user/levi.service
[Unit]
Description=LEVI workbench
After=network-online.target

[Service]
WorkingDirectory=/path/to/LEVI
Environment=LEVI_WORKSPACE=/path/to/workspace
ExecStart=/path/to/uv run --no-sync levi serve
# levi stop refuses while exports, conversions or model jobs run; --wait lets them finish.
ExecStop=/path/to/uv run --no-sync levi stop --wait 30
TimeoutStopSec=1900
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now levi
loginctl enable-linger "$USER"     # keep it running after logout (may need an administrator)
```

**Access from another machine.** LEVI has no login: it is a single-user workbench with file access. Keep it on `127.0.0.1` and forward the port (`ssh -L 7860:127.0.0.1:7860 user@server`; 7861 is the internal API, not the workbench). To share it, put it behind an authenticating reverse proxy that forwards to `127.0.0.1:7860`, set `LEVI_SECURE_COOKIES=1` under HTTPS, and never bind `--host 0.0.0.0` on an open network.

**Updating: stop, install, start.** While this checkout's LEVI runs, every install step follows this order, the Python dependencies (`uv sync`) included: `git pull`; `uv run --no-sync levi stop` (plain `uv run` syncs the environment before the command runs, which changes packages under the running service; `levi stop` refuses while jobs run: `--wait` lets them finish; `--force` kills them); `uv run --locked levi install --profile core` (it syncs the dependencies and rebuilds when the frontend sources changed; `levi doctor` reports a stale build); start the service again. `levi install` changes nothing a running LEVI of this checkout uses: when port 7860 listens, a `levi serve` of the checkout runs or a live service of the checkout runs (`levi live start`, its daemon), the Python dependencies, the frontend dependencies (`node_modules`) and the build (`.next`) are handed back as a person's steps after "stop the running LEVI first" (`stop-service`, which also names `uv run --no-sync levi live stop` for a live service). A build made before the source stamp existed is "unknown" and is rebuilt only with `--yes`. If you also run the live service (`levi live`), restart it together with the product LEVI after an update: both read the same code, and new `live.toml` keys (for example `vllm.model`, `gpu.lock_unavailable`) take effect only in a new service.

## 5. The live annotation service

`levi live` labels robot rollouts in the background while an evaluation writes them ([Live](docs/LIVE.md)). On a new machine:

1. Build the vLLM environment and download the weights ([vLLM](docs/VLLM.md) section 1); the default launcher is the shipped `scripts/vllm/serve.sh`.
2. `uv run levi live init --root /path/to/rollouts` writes `~/.levi-live/workspace/live.toml` (no default names a machine's folder; set `[fr3] health_file` and `[gpu] lock_file` if you have them).
3. `uv run levi live doctor` checks the scripts, the pid folder, the GPU lock file, the roots and paths copied from another machine.
4. `uv run levi live once --fake-vlm --workspace /tmp/levi-live-smoke/ws --home /tmp/levi-live-smoke/home --root /path/to/rollouts` runs the pipeline once against a stand-in model (no GPU), then `uv run levi live start --daemon`.

## 6. What is where

| | |
| --- | --- |
| `.venv/`, `.runtime/` (Bun, uv's Python), `node_modules/`, `.next/` | inside the checkout; rebuilt by `levi install`. Not relocatable: after moving the checkout, run `uv sync` again |
| `$LEVI_WORKSPACE` | datasets, LEVI's state (`outputs/LEVI/`), weights (`checkpoints/`), caches ([Workspace](docs/WORKSPACE.md)) |
| `integrations/*/.venv`, `.venv-vllm/` | worker and vLLM environments |
| `~/.levi-live/` | the live service's status, pid file and default workspace |

`levi clean` previews regenerable caches (`--apply` removes them, with the service stopped). The Docker image (`Dockerfile`) covers the core only and has not been built by the maintainers.
