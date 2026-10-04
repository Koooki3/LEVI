# Installing LEVI: instructions for an AI agent

For Claude Code, Codex or any agent with a shell that installs LEVI for a person. The person-facing guide is [INSTALL.md](INSTALL.md) ([中文](INSTALL.zh-CN.md)); this page is the order of commands, how to check each step, and what to hand back to the person. If you are changing LEVI's code instead, read [AGENTS.md](AGENTS.md).

## Rules

- **Plan first, then act.** Run `levi install --plan --json` and show the person what will happen (downloads, network, steps that need them) before running anything that downloads more than a few hundred MiB.
- **Never do a person's step.** Steps with `"kind": "human"` need root (`sudo`), a token, a licence, or a choice. Do not run `sudo` unless the person asked you to; do not create, read, print or copy tokens (`HF_TOKEN`, `.env` values, `human.key`, connection grants); do not accept a licence; do not choose which datasets an agent may see. List these steps for the person (below).
- **Do not touch what runs.** Never stop or restart a LEVI someone is using (`levi stop` refuses while jobs run; never pass `--force` on your own), never connect to ports 5000 or 8000 (robot and policy servers), and never start GPU work the person did not ask for.
- **Use `--json` and exit codes**, not the human text: both commands print one JSON document on stdout.

## Order

| # | Command | Check |
| --- | --- | --- |
| 1 | `command -v git uv ffmpeg ffprobe` | each prints a path; a missing `uv` or `ffmpeg` is a person's step (root, or a download they approve) |
| 2 | `git clone https://github.com/Koooki3/LEVI.git && cd LEVI` | `git status` is clean |
| 3 | `export LEVI_WORKSPACE=<folder the person chose>` | path short (the core's socket path must stay within 103 bytes; doctor checks it) |
| 4 | `uv run --locked levi install --profile core --plan --json` | exit 0 (nothing to do), 1 (automatic steps left) or 10 (a person's steps left); read `steps[]`. A `stop-service` step means this checkout's LEVI is running: the frontend is not rebuilt under it; ask the person to stop it (`uv run levi stop`), never stop it yourself |
| 5 | `uv run --locked levi install --profile core --json` | exit 0, or 10 with only person steps left; `steps[].result` |
| 6 | `uv run levi doctor --json` | `status` `ok` or `warn`; every `fail` has a `fix` |
| 7 | optional profiles: `uv run levi install --profile <name> --plan --json`, then without `--plan` (add `--yes` only after the person agreed to the download) | as 4-6 |
| 8 | acceptance (below) | all pass |

`levi install` is idempotent: run it again after the person finished their steps, and it only does what is still missing. Updating an installed LEVI is always **stop, install, start**, and the person decides when the running service stops. A `build` step with `needs_yes` and a `confirm` reason (a build without a source stamp) is rebuilt only with `--yes`, and only while nothing serves the checkout. With `--json`, the steps' own output (uv, bun, the build) goes to standard error; standard output is one JSON document.

## The JSON

`levi install --plan --json` (schema `levi.install.plan.v1`):

```json
{"schema": "levi.install.plan.v1", "profiles": ["core", "sam3"], "workspace": "/data/levi",
 "steps": [
  {"id": "build", "profile": "core", "title": "the production build of the web UI (a few minutes)",
   "kind": "auto", "command": ["/…/.venv/bin/python", "-m", "levi.cli", "build"], "cwd": "/…/LEVI",
   "network": false, "download_bytes": 0, "download": "", "sudo": false, "token": false,
   "consent": false, "done": false, "needs_yes": false, "note": "…", "docs": ""},
  {"id": "sam3-weights", "profile": "sam3", "kind": "human", "token": true, "consent": true,
   "command": ["HF_HOME=\"/data/levi/.cache/huggingface\" hf auth login"], "docs": "docs/SAM3.md", "…": "…"}
 ]}
```

Without `--plan`, the result (schema `levi.install.result.v1`) has the same steps with `result` (`done`, `already done`, `for a person`, `waiting for --yes (…)`, `failed: …`, `skipped (…)`), `exit_code`, and the doctor report under `doctor`.

`levi doctor --json` (schema `levi.doctor.v1`):

```json
{"schema": "levi.doctor.v1", "status": "human", "exit_code": 10,
 "checks": [
  {"id": "ffmpeg", "group": "system", "level": "fail", "message": "ffmpeg is not on PATH: …",
   "fix": "sudo apt install ffmpeg (or …)", "human": true},
  {"id": "build", "group": "frontend", "level": "warn",
   "message": "the production build is stale: …", "fix": "uv run levi build (then restart the service)", "human": false}
 ]}
```

`level` is `ok`, `info` (optional part not set up), `warn` or `fail`; `human: true` means the fix is a person's. Ports to probe are options: `--vllm-ports 8100` (default), `--ollama-ports 11435,11434`, `none` to skip; 5000 and 8000 are refused.

| Exit | `levi install` | `levi doctor` |
| --- | --- | --- |
| 0 | everything done | all ok |
| 1 | with `--plan`: only automatic steps are left | warnings an agent may fix or leave |
| 2 | a step failed (read its `result`) | a failure an agent can fix (run its `fix`) |
| 10 | steps left for a person, or for `--yes` | what is left needs a person |

## Handing steps to the person

Collect every step with `"kind": "human"` and `"done": false`, and every doctor check with `"human": true` and level `warn`/`fail`. Write them for the person as a numbered list: what to do (the `command`), why (`title`, `note`), what it costs (`download`, root, token, licence), and where to read more (`docs`). Then stop and wait; after they say it is done, run `levi install … --json` and `levi doctor --json` again to confirm. Never ask them to paste a token into the chat: a token goes into their own shell (`HF_TOKEN`) or the web UI.

## Acceptance

```bash
uv run levi doctor --json                       # status ok or warn
mkdir -p "$LEVI_WORKSPACE/tmp/build"
uv run pytest -q tests/test_install_doctor.py tests/test_live_portable.py tests/test_vllm_launcher.py \
  tests/test_startup.py --basetemp="$LEVI_WORKSPACE/tmp/build/pytest"   # needs: uv sync --group dev
mkdir -p /tmp/levi-smoke/rollouts               # the live pipeline once, stand-in model, no GPU
uv run levi live once --fake-vlm --workspace /tmp/levi-smoke/ws --root /tmp/levi-smoke/rollouts \
  --home /tmp/levi-smoke/home --max-seconds 60  # exit 0
uv run levi                                     # in the background, e.g. nohup setsid … &
curl -s http://127.0.0.1:7861/api/levi/health   # {"service": "levi-api", …}
```

Only start the service (`uv run levi`) when the person wants it running and ports 7860/7861 are free (doctor: `ui-port`, `api-port`). Remove `/tmp/levi-smoke` afterwards. The full test suite (`uv run pytest`) takes a while; run it one at a time with its own `--basetemp`.
