# Local models (Ollama)

[中文](OLLAMA.zh-CN.md)

LEVI can run a vision-language model on your own machine through [Ollama](https://ollama.com). The default profile is `qwen3.5:4b` (Qwen3.5-4B, Q4_K_M, about 3.4 GB, image input). A local model works through the same approved plans, evidence validation, review and commit as any other agent — it proposes, a person publishes — with no API key and no cloud fallback. LEVI meters every request.

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

## Development checks

```bash
uv sync --locked --group dev --extra agent
mkdir -p "$LEVI_WORKSPACE/tmp/validation"
uv run pytest tests/test_ollama_protocol.py tests/test_ollama_integration.py \
  tests/test_ollama_runtime.py tests/test_gpu_guard.py tests/test_teaching.py \
  --basetemp="$LEVI_WORKSPACE/tmp/validation/local-model-tests"
```

These tests use fake model responses, simulated teachers and a fake `nvidia-smi`. They do not measure model quality.

## Limits

- The annotation quality of a small local model is not established. Measure it on your data with a teacher and a reference before relying on it.
- `OLLAMA_NO_CLOUD` is not network isolation: there is no OS-level offline sandbox.
- The GPU guard sees processes through `nvidia-smi`; it is a courtesy policy, not a GPU scheduler or lease.
- No weight training, automatic skill publication or competency promotion.
