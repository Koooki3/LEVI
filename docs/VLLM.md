# vLLM for LEVI

LEVI talks to vLLM in two ways. [中文](VLLM.zh-CN.md)

- **The product LEVI** never starts vLLM. You run a server yourself and bind an `openai-local` profile to it ([Local models](OLLAMA.md#8-a-local-openai-compatible-server-vllm)).
- **The live annotation service** (`levi live`, [Live](LIVE.md)) starts, sleeps, wakes and stops its own vLLM through a launch script. This page describes that script's contract, the launcher shipped with LEVI (`scripts/vllm/serve.sh`) and how to build a vLLM environment for it.

## 1. Building a vLLM environment

vLLM lives in an environment of its own, never in LEVI's `.venv` (it pins its own torch and CUDA wheels).

| | Verified |
| --- | --- |
| Platform | Linux x86_64, one NVIDIA GPU |
| vLLM | 0.30.0 (Python 3.12), torch 2.13.0+cu132, flashinfer-python 0.6.18.post1, xgrammar 0.2.7, transformers 5.17.0 |
| Driver | an NVIDIA driver that supports the CUDA version of the torch wheel (CUDA 13.2 for `+cu132`; `nvidia-smi` shows the highest CUDA version the driver supports) |
| Card | the measured profile (Qwen3.8-27B INT4, 49152-token context, 128 images, two sequences) needs a 32 GB card; a smaller card needs a smaller model or context, and the `[vllm]` budget settings recalibrated |

Other versions may work; the launch arguments LEVI passes (section 3) use vLLM 0.30's names.

```bash
cd /path/to/LEVI
uv venv --python 3.12 .venv-vllm               # the launcher's default location
uv pip install --python .venv-vllm/bin/python "vllm==0.30.0"
scripts/vllm/serve.sh --check                  # resolved settings; exit 1 if vllm or the model is missing
```

`LEVI_VLLM_VENV` points the launcher at an environment elsewhere; without one, it uses `vllm` on `PATH`.

**No system CUDA toolkit.** FlashInfer compiles kernels the first time they run and looks for `nvcc` under `CUDA_HOME`; without a toolkit the server dies at engine start (`Could not find nvcc ... /usr/local/cuda`). No root is needed:

1. Install the pip CUDA toolchain wheels pinned to torch's CUDA version (a plain `pip install vllm` can pull newer compiler wheels than the runtime and driver): `uv pip install --python .venv-vllm/bin/python "cuda-toolkit[nvcc,crt,nvvm,cccl,cudart]==<torch.version.cuda>.*"`. Repeat after any upgrade that changes the `nvidia-*` wheels.
2. `scripts/vllm/make-cuda-home.sh` builds a `CUDA_HOME` of symbolic links into those wheels (with the driver's `libcuda.so.1` as the link-time stub) at `.venv-vllm/cuda-home`.
3. `export LEVI_VLLM_CUDA_HOME=$PWD/.venv-vllm/cuda-home`: the launcher exports `CUDA_HOME` from it and puts the environment's `bin` (FlashInfer runs `ninja`) on `PATH`.

**Weights.** Download them before the first start (the default model is about 19 GiB): `HF_HOME=<cache> hf download RedHatAI/Qwen3.8-27B-INT4`. Pass the same `HF_HOME` to the service (it reaches the launcher unchanged) and `HF_HUB_OFFLINE=1` so a start never downloads. A gated model needs a Hugging Face token (`HF_TOKEN`), which a person provides.

## 2. The shipped launcher: `scripts/vllm/serve.sh`

The live service's default `[vllm] script` and `stop_script` (a relative path in `live.toml` is relative to the LEVI checkout). It has no machine paths: the checkout is found from the script's own location, everything else comes from the environment.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LEVI_VLLM_MODEL` | (set by `levi live` from `[vllm] model`) | model id or local snapshot to serve; required to launch |
| `LEVI_VLLM_SERVED_NAME` | the model | `--served-model-name` (`levi live` sets it from `[vllm] served_model`) |
| `PORT` | 8100 | port, bound to `VLLM_HOST` (default 127.0.0.1) |
| `GPU_UTIL` | 0.90 | `--gpu-memory-utilization` |
| `MAX_MODEL_LEN` | unset | `--max-model-len` (LEVI passes it as an argument instead) |
| `LEVI_VLLM_PID_DIR` | `${LEVI_LIVE_HOME:-~/.levi-live}/vllm` | pid file and logs (`levi live` sets it from `[vllm] pid_dir`) |
| `LEVI_VLLM_VENV` | `<checkout>/.venv-vllm` | the vLLM environment |
| `LEVI_VLLM_CUDA_HOME` | unset | a `CUDA_HOME` to export (see above) |
| `VLLM_WAIT_S` | 600 | seconds to wait for `GET /health`; 0 returns at once (LEVI sets 0) |
| `SERVE_SKIP_PREFLIGHT`, `SERVE_DRY_RUN` | 0 | skip the free-VRAM check; print the command only |

`HF_HOME`, `HF_HUB_OFFLINE`, `HF_TOKEN`, `CUDA_VISIBLE_DEVICES` and `VLLM_*` pass through. `--stop` signals only a pid file that holds one positive number, and only a process whose command line contains `vllm` (a stale pid file whose number was reused names somebody else's process: it is left alone and the file removed). A live process whose command line cannot be read is not signalled either, and its pid file stays. Before launching, the script refuses (exit 3) when `nvidia-smi` shows less free memory than the budget needs (and prints who holds the GPU), or when `nvidia-smi` cannot be read (it says so). Run by hand:

```bash
LEVI_VLLM_MODEL=RedHatAI/Qwen3.8-27B-INT4 LEVI_VLLM_SERVED_NAME=qwen3.8-27b \
  scripts/vllm/serve.sh --max-model-len 32768 --limit-mm-per-prompt '{"image":64,"video":0}'
scripts/vllm/serve.sh --stop 8100
```

## 3. The contract between `levi live` and a launch script

Any script that keeps this contract can replace the shipped one (`[vllm] script`, `stop_script`, `pid_dir` in `live.toml`; `levi live doctor` checks that both scripts exist and are executable and that the pid folder can be written). The code is `levi/live/gpumgr.py` (`Vllm.start`, `Vllm.stop`, `script_env`, `launch_args`).

**Launch.** LEVI runs `<script> <arguments>` in a new session, with standard input closed and output appended to `<workspace>/live/logs/vllm-launch.log`, and waits up to 120 s for it to exit. The environment is LEVI's own plus:

| Variable | Value |
| --- | --- |
| `PORT` | `[vllm] port` |
| `GPU_UTIL` | the budget chosen for this start (`gpu_memory_utilization`, from free VRAM) |
| `VLLM_WAIT_S` | `0`: return as soon as the server process is running; LEVI polls `/health` itself (up to `start_timeout_s`) |
| `LEVI_AGENT` | `[gpu] lock_agent` |
| `VLLM_SERVER_DEV_MODE` | `1` when `sleep_mode` is on (it enables the sleep endpoints) |
| `LEVI_VLLM_PID_DIR`, `LEVI_VLLM_MODEL`, `LEVI_VLLM_SERVED_NAME` | `[vllm] pid_dir`, `model` (when set), `served_model`; a script that serves a fixed model may ignore them |

The arguments, which must reach `vllm serve` after the script's own (later arguments win):

```
--structured-outputs-config {"backend":"xgrammar","disable_any_whitespace":true}
--max-model-len <context chosen for this start>
--limit-mm-per-prompt {"image":<max_images>,"video":0}
--max-num-seqs <max_num_seqs>
--max-num-batched-tokens <max_num_batched_tokens>
--override-generation-config {"temperature": <temperature>}
--enable-sleep-mode                     (when sleep_mode is on)
```

The script must exit 0 once the server is started, and before exiting write the server's pid to **`<pid_dir>/vllm_<PORT>.pid`** (one decimal number; it must be the process-group leader, as `setsid` makes it, because LEVI signals the group). A non-zero exit or a missing pid file is a failed start (retried with back-off, then left for a person: `levi live resume`). If the GPU lock is held, its descriptor is inherited by the script and the server (`pass_fds`): the lock lasts as long as the server, even if the service dies. The script must not close it.

**The server** must bind the loopback interface, serve `[vllm] served_model` as its model name and answer `GET /health` (200 when ready). With sleep mode, LEVI uses vLLM's development endpoints `POST /sleep?level=1`, `POST /wake_up` and `GET /is_sleeping` (vLLM 0.30 serves them with `--enable-sleep-mode` and `VLLM_SERVER_DEV_MODE=1`). `levi live` asks the worker's model calls for `/v1/chat/completions` with JSON-schema structured output.

**Stop.** LEVI runs `<stop_script> --stop <PORT>` (same environment, 120 s timeout). The script should signal the process group in the pid file (TERM, then KILL after a grace period) and remove the pid file; its exit code is not used. If the recorded process is still alive afterwards, LEVI sends TERM and then KILL to the group itself, and keeps the GPU lock until no process of the group is left on the GPU. LEVI only ever stops a server it started: the pid and its process identity (start time) are recorded in `<workspace>/live/vllm.json`.

**Not part of the contract**: the model's weights and cache location, CUDA setup, logs beyond the pid file. The script may print anything.

## 4. Checks

- `scripts/vllm/serve.sh --check` prints what it would run and fails when vLLM or the model is not set.
- `levi live doctor` reports a missing or non-executable script, an unwritable pid folder, a GPU lock file that cannot be opened, missing rollout roots and paths that point into another user's home.
- `levi doctor` (the installation check, [Install](../INSTALL.md)) asks a server on port 8100 (or the ports given with `--vllm-ports`; 5000 and 8000 are refused) for `/health` and `/v1/models`, on the loopback interface only and without following redirects. It does not read the live service's `[vllm] port`; pass it with `--vllm-ports` if you changed it.
- `tests/test_vllm_launcher.py` runs the shipped launcher against a stand-in `vllm` executable and checks the whole contract (launch, pid file, arguments, stop) without a GPU.
