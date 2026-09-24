# Local models (Ollama, or a local vLLM server)

[中文](OLLAMA.zh-CN.md)

LEVI can run a vision-language model on your own machine through [Ollama](https://ollama.com), or through a local OpenAI-compatible server such as [vLLM](https://docs.vllm.ai) for models Ollama does not carry (see [§8](#8-a-local-openai-compatible-server-vllm)). The default profile is `qwen3.5:4b` (Qwen3.5-4B, Q4_K_M, about 3.4 GB, image input). A local model works through the same approved plans, evidence validation, review and commit as any other agent — it proposes, a person publishes — with no API key and no cloud fallback. LEVI meters every request.

Local models are used for two things:

- **Annotation**: coarse and refined phases of a planned task, as the model in an online run.
- **Natural-language tasks**: turning a sentence into a checked task spec (`levi agent task new "…"`, or the task console in the Workbench). See [Agents → Natural-language tasks](AGENTS.md#natural-language-tasks).

## 1. Install Ollama

LEVI does not install binaries or download models on its own. Install Ollama with its [official instructions](https://docs.ollama.com/download), or, without root, unpack the release archive into a directory you own and put `ollama` on `PATH`:

```bash
mkdir -p ~/tools/ollama && cd ~/tools/ollama
curl -L -o ollama.tar.zst https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst
tar --use-compress-program=unzstd -xf ollama.tar.zst
ln -sf "$PWD/bin/ollama" ~/.local/bin/ollama
ollama --version
```

## 2. Choose who runs the service

**LEVI-owned service (recommended).** Agent Workbench → Accounts & connections → *LEVI-owned Ollama service*: check installation, choose a port (default 11435), authorize and start. Or, from a script with the operator credential:

```bash
curl -X POST -H "x-levi-ui-token: $(cat "$LEVI_WORKSPACE/outputs/LEVI/workbench/agent/core/human.key")" \
  -H "Content-Type: application/json" -d '{"port": 11435, "approve_start": true}' \
  http://127.0.0.1:7861/api/levi/agent/v1/ollama/runtime/start
```

Models are stored in `$LEVI_WORKSPACE/checkpoints/ollama`; the process record and log in `outputs/LEVI/workbench/models/ollama/`. The service runs with `OLLAMA_NO_CLOUD=1`, one parallel request and one loaded model. It survives a LEVI restart and is stopped explicitly (`POST …/ollama/runtime/stop`).

**Your own service.** Model settings → *Ollama · local service*, base URL `http://127.0.0.1:11434`, model `qwen3.5:4b`, explicit loopback opt-in. Remote (non-loopback) Ollama endpoints are not supported.

## 3. Download and bind the model

1. **Inspect local models** reads the service's inventory and declared capabilities; it runs no inference.
2. If the model is missing, **Download model explicitly** after reviewing its license (the [Qwen3.5-4B card](https://huggingface.co/Qwen/Qwen3.5-4B)). Progress is reported per layer; closing the page does not cancel it. `ollama pull qwen3.5:4b` against the same service and model directory works too.
3. **Bind installed model** records the exact digest, template, version and capabilities (confirm structured output and image input). A changed model or profile requires a new task plan; a missing model is never pulled during a task.

## 4. Shared GPUs: the guardian

On a machine where others train or run robots on the same GPU, sharing it silently slows their inference and control loop and can run them out of memory. LEVI's GPU guardian yields, and learns how to yield no longer than needed:

- **Before every local-model request** (and before starting the LEVI-owned service) it samples `nvidia-smi` and decides: `free`, `shared`, `cooling` or `busy`. A blocked run stops at that boundary with the reason and expected wait, marked `blocked_by: gpu`.
- **Policy per workload** (`workbench/models/gpu-policy.json`): `protect` (default — yield entirely), `share` (coexist while memory and utilisation leave room) or `ignore` (e.g. a display server). Ollama's own `llama-server` is recognised by install path and parent process, never by name.
- **Learned quiet window**: it records when each workload (by a signature that survives restarts with new checkpoints) appears and leaves, and waits 1.5× the 90th-percentile restart gap before using the GPU after it leaves (60–1800 s; default `LEVI_GPU_QUIET_SECONDS=300` until it has seen enough restarts).
- **While the service runs** a watcher samples every 15 s (2 s while a model is resident), unloads resident models the moment protected work appears — a request cut off this way is recorded as a preemption, not a model failure — and **resumes** every run blocked by the GPU once the window has passed.
- `uv run levi agent gpu` (or the `gpu.status` capability) shows the decision, its reason, the workloads seen and their learned windows.
- `LEVI_GPU_SHARING=allow` disables the guardian when you have agreed to share the GPU.

## 5. Run a task

Select the profile in a new task (or pass `--provider qwen-local` to `levi agent task new`), choose a small episode and camera scope, and give explicit evidence-sharing consent. Approve the plan, run the pilot, review it, then run the rest. The model receives time-labelled evidence images plus bounded context; the schema and scope validators reject malformed or out-of-scope output. Tokens and time are metered per phase; a phase's result and cost are settled before any teacher gate, so a resumed run never pays twice.

Pausing or cancelling a run cuts its model request in flight, so the GPU stops computing an answer nobody will use. After LEVI's last request the model stays loaded for `LEVI_OLLAMA_KEEP_ALIVE` (default `2m`; Ollama's own default is 5 minutes), then Ollama unloads it. See [Workspace](WORKSPACE.md#processes-levi-starts-and-how-they-stop) for every process LEVI starts and how it stops.

The first request after the model was unloaded includes loading it (about 20 s for `qwen3.5:4b` on a consumer GPU). Schema-bound calls are sent with reasoning ("thinking") off, since it costs tokens and time the structured answer rarely needs.

## What the harness enforces for a small model

A 4B model follows instructions loosely; LEVI does not rely on it to. Each request's output schema is narrowed so that malformed answers cannot be decoded (Ollama enforces the schema while decoding):

- only the annotation kinds the plan allows (a plan with subtask definitions asks for intervals only), only its subtask ids, and only the evidence ids it was shown or its draft already cited;
- every interval has an end, an outcome and a one-sentence evidence note; the proposals come first and the summary is short, so the answer is not spent on narration;
- a refinement returns the draft's intervals one for one, each keeping its subtask; a refined boundary that moves further than the plan's boundary window (beyond the frames it was shown) keeps the draft's, intervals keep their order and shared boundaries;
- the answer budget grows with the number of intervals expected.

The request is sized to the model: the compact evidence rows sent (id, image position, time), the images a refinement carries, and the per-image and per-character token cost — learned from this model's own metered requests (`workbench/models/request-cost/<provider>.json`, calibrated once with two one-token calls). A long episode is refined in consecutive batches that each fit the context and the plan's frame cap. An answer that still fails keeps its valid proposals: under a teacher the teacher sees them with the reason; without one the invalid ones are dropped and recorded (`answer_salvaged`). When nothing in an episode's answer holds up, that episode is set aside (`run.failed`, event `episode_set_aside`, the rejected answer kept beside the run) and the run goes on; a resumed run does not retry it. The pilot still blocks, and so does a run once more than 10 % of its episodes (at least 3) are set aside, since that points at the setup rather than at a few hard episodes. Tokens are settled whether or not the answer holds up.

## 6. What the model learns from

A local model learns nothing between tasks except through its prompt, so LEVI puts a bounded **brief** into every request, frozen at plan time:

- the dataset profile, subtask vocabulary and recurring uncertainty from committed work;
- a teacher's notes for this dataset and workflow, and notes about using LEVI;
- examples from **other** episodes — never the ones being annotated;
- lessons from published harness improvements;
- LEVI's built-in, dataset-agnostic annotation rules (`levi/knowledge/annotation.md`; see [Built-in knowledge](KNOWLEDGE.md)).

Model weights are never changed.

## 7. A teacher in the loop

A task can put a scoped external agent (Codex, Claude Code) in front of each learner phase:

```bash
uv run levi agent connect --client claude --project /path/to/teacher/project --dataset local/my_dataset --apply
uv run levi agent task new "…" --provider qwen-local --supervision supervised --teacher <grant-id>
```

In the Workbench, choose the connection under **External teacher supervision**. The teacher reads each phase with `supervision.pending`, looks at the real images with `evidence.read`, and answers:

```json
{
  "name": "supervision.feedback",
  "arguments": {
    "run_id": "temporal-20260922T1303",
    "teaching_id": "temporal-20260922T1303:0:coarse",
    "revision": 0,
    "decision": "revise",
    "note": "A plate released onto a different colour is a failure, not a success.",
    "output": { "summary": "…", "proposals": ["…"], "warnings": [] }
  }
}
```

`accept` keeps the learner's output; `revise` replaces it with a complete, validated output; `reject` leaves the phase blocked. Feedback is bound to the exact phase revision and input; only the assigned connection or a person can submit it, and it never approves a plan, a pilot or a publication.

Every piece of feedback is kept as a file (`datasets/<name>/teaching/<run>/episode_NNNNNN-<phase>.json`) and its note becomes part of the dataset's memory for the next task. A teacher can also correct a natural-language interpretation (`tasks.feedback`); the corrected spec becomes an example and the note a lesson for future requests. A teacher's committed work can be frozen as a reference and later learner runs graded against it (`teaching/reference-<task>.json`, `teaching/grades.json`).

## 8. A local OpenAI-compatible server (vLLM)

A profile of kind `openai-local` runs a model served by vLLM (or another local OpenAI-compatible server) through the same hardened path as Ollama: the narrowed answer schema (sent as `response_format` `json_schema`, which the server enforces while decoding), compact evidence rows, request-cost calibration and image limits, salvage of invalid answers, pause/cancel cutting the request in flight, the GPU guardian and natural-language tasks. Evaluation records name it `local-vlm`. Use it for models without an Ollama build — Molmo2 — or to compare a model on both engines.

LEVI does not install, start or stop the server. Run vLLM in an environment of its own, not LEVI's `.venv`. The commands below are for vLLM 0.30:

```bash
# Qwen3.8-27B, 4-bit weights (sized for a 32 GB GPU)
vllm serve RedHatAI/Qwen3.8-27B-INT4 --served-model-name qwen3.8-27b \
  --host 127.0.0.1 --port 8100 --max-model-len 32768 --max-num-seqs 2 \
  --gpu-memory-utilization 0.90 --limit-mm-per-prompt '{"image": 64, "video": 0}'

# Molmo2-8B (Molmo2-ER is served the same way from its own checkpoint, --max-model-len 16384).
# On a 32 GB GPU the multimodal memory profile (26 full-size images) runs out of memory:
# skip it, leave headroom for the vision encoder, and send downscaled frames
# (profile "image_max_side": 378, one crop per frame). Molmo2 decodes greedily.
vllm serve allenai/Molmo2-8B --trust-remote-code --served-model-name molmo2-8b \
  --host 127.0.0.1 --port 8101 --max-model-len 36864 --max-num-batched-tokens 36864 \
  --max-num-seqs 1 --gpu-memory-utilization 0.82 --skip-mm-profiling \
  --override-generation-config '{"temperature": 0.0}' \
  --limit-mm-per-prompt '{"image": 64, "video": 0}'
```

Then create the profile (Workbench → Model settings → *Local OpenAI-compatible server (vLLM)*, or REST):

```bash
curl -X POST -H "x-levi-ui-token: $(cat "$LEVI_WORKSPACE/outputs/LEVI/workbench/agent/core/human.key")" \
  -H "Content-Type: application/json" http://127.0.0.1:7861/api/levi/agent/v1/providers -d '{
  "name": "qwen38-vllm", "kind": "openai-local", "base_url": "http://127.0.0.1:8100",
  "model": "qwen3.8-27b", "context_tokens": 32768, "vision": true,
  "allow_localhost": true, "max_images": 64}'
```

and inspect and bind it in Accounts & connections (`GET …/providers/qwen38-vllm/ollama`, then `POST …/ollama/bind` with the digest shown, `"vision": true, "structured_output": true`).

- **Base URL** is the server root (`http://127.0.0.1:8100`, no `/v1`; LEVI's examples use 8100 because openpi's policy server defaults to 8000), loopback only.
- **`model`** is the `--served-model-name`. The bound digest identifies what the server says it serves: the model id, the weights it was started from (`root` in `/v1/models`) and its context. A server restarted with other weights blocks inference until the model is bound again. To pin an exact revision, serve a local snapshot directory: its path carries the revision.
- **`context_tokens`** is set by binding to the server's `--max-model-len` (at most 131072; binding refuses a longer one). The server's context is fixed when it starts and no request can narrow it, so it is what one call may spend and what LEVI reserves for it.
- **`max_images`** is the server's `--limit-mm-per-prompt` image count. A request with more images is refused before it is sent, and refinement batches are sized to it. **`image_max_side`** (optional) sends each image scaled down to at most that many pixels on its longer side; the evidence files keep their native size, and calibration prices the scaled images.
- **Thinking** is off: every request sends `chat_template_kwargs: {"enable_thinking": false}`, which Qwen3-family templates obey and others ignore. `think: true` opts in, but only works with a `--reasoning-parser` on the server (the schema then applies after the reasoning), and the reasoning comes out of the same output allowance. If the server returns reasoning and no answer, the request fails with that reason.
- **`fold_system: true`** is needed for Molmo2: its chat template refuses a system turn and requires strict user/assistant alternation, so the system prompt is sent at the start of the first user turn. Every image goes before the text, in evidence order (image *n* is the evidence row numbered *n*), as Molmo2's template places them anyway. Molmo2 has no tool calling; LEVI does not use it.
- **`prompt_style: "lean"`** (both local kinds) sends the plan's instruction as written plus a frame list ("image *n* = *t* s") instead of LEVI's skills and one JSON document, keeps only teacher notes and lessons from memory (never a second copy of the instruction), and asks for intervals without citations: LEVI cites up to three frames inside each interval itself and uses the interval's wording as its evidence note. Paired runs of the same model with and without LEVI showed the full prompt costing a local model segment F1 and about twice the output tokens. Default `full`.
- **Structured output**: vLLM's default backend (`auto`) compiles every LEVI answer schema; if a request is refused with a structured-output error, start the server with `--structured-outputs-config.backend guidance`.
- **Sampling** follows the model's own generation config (vLLM's default `--generation-config auto`); LEVI sends no temperature. Do not pass `--generation-config vllm`, which replaces the model's recommended settings with vLLM's defaults.
- **Key**: none is needed. If the server runs with `--api-key`, set it in `LEVI_LOCAL_MODEL_KEY` (the profile's default `key_env` for this kind, so a cloud key is never sent to a local port) or as a session credential.
- **GPU guardian**: the processes serving the profile's port (the API server that listens there and the engine processes it started) are recognised as LEVI's own model through the listening socket, never by name, so they do not block this profile's requests. Other people's work still does. The guardian cannot unload a vLLM model: the server holds `--gpu-memory-utilization` of the GPU for as long as it runs. For an Ollama profile, and for a profile on another port, it is someone else's work: Ollama's models are unloaded for it, a run waits until it has gone, and the guardian resumes each blocked run only when the GPU is clear for that run's own profile. Stop vLLM before running Ollama, and do not run both at once. A profile's requests take whatever listens on its port for its server; while another program uses that port (openpi's policy server also defaults to 8000), disable the profile. Load and unload controls and downloads do not apply (HTTP 409). Recognition reads the server's sockets through `/proc`, so it needs the server to run as the same user as LEVI; otherwise add an ignore rule to `workbench/models/gpu-policy.json`, e.g. `{"rules": [{"match": "(?i)vllm", "class": "ignore"}]}`.
- **Tokens**: vLLM reports the whole prompt, while Ollama leaves out a cached prefix; compare completion tokens and per-call time across engines. Ollama calls also record `load_seconds`, `prefill_seconds` and `decode_seconds` in each phase's usage.

Before a long run, run the pilot: binding performs no inference, so the pilot is the first request the server answers.

## Development checks

```bash
uv sync --locked --group dev --extra agent
mkdir -p "$LEVI_WORKSPACE/tmp/validation"
uv run pytest tests/test_ollama_protocol.py tests/test_ollama_integration.py \
  tests/test_ollama_runtime.py tests/test_gpu_guard.py tests/test_teaching.py \
  tests/test_openai_local.py \
  --basetemp="$LEVI_WORKSPACE/tmp/validation/local-model-tests"
```

These tests use fake model responses, simulated teachers and a fake `nvidia-smi`. They do not measure model quality.

## Limits

- The annotation quality of a small local model is not established. Measure it on your data with a teacher and a reference before relying on it.
- `OLLAMA_NO_CLOUD` is not network isolation: there is no OS-level offline sandbox.
- The GPU guard sees processes through `nvidia-smi`; it is a courtesy policy, not a GPU scheduler or lease. It recognises a local server's processes only when they run as the same user as LEVI (it reads their sockets through `/proc`).
- No weight training, automatic skill publication or competency promotion.
