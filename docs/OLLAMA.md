# Local Ollama and external teacher supervision

[中文指南](OLLAMA.zh-CN.md)

This integration connects a native Ollama service to the **existing** LEVI plan,
evidence, annotation validation, review and commit workflow. It does not change
Qwen weights, provide autonomous promotion, or replace dataset conversion.
Generic recipe execution and the wider 0.4.0 architecture are still being migrated;
see [implementation status](architecture/IMPLEMENTATION_STATUS.md).

## 1. Prepare LEVI

From the cloned repository:

```bash
export LEVI_WORKSPACE="$PWD/.state"   # or your existing workspace
uv sync --locked --extra agent
uv run levi setup
uv run levi build
uv run levi
```

Open the **Web** URL printed by the launcher. Do not open the backend port.
Install Ollama separately using its [official installation instructions](https://docs.ollama.com/download).
LEVI does not install binaries, pull models, probe accelerators or run inference
when a model settings page opens.

## 2. Choose who owns the service

**Existing service:** In Agent Workbench → Model settings, choose **Ollama · local
service**, set a profile name, `http://127.0.0.1:11434`, and `qwen3.5:4b`. Enable the
explicit loopback option and save. No API key is required. The service owns its
model path; LEVI reports it as external instead of inventing a checkpoint path.
Remote non-loopback Ollama endpoints are not supported by this adapter yet.

**Dedicated service:** In Accounts & connections → LEVI-owned Ollama service,
check installation, choose a free port (default 11435), explicitly authorize the
operation, and start. Starting the real service may initialize hardware. Model
blobs go to `$LEVI_WORKSPACE/checkpoints/ollama`; process state and logs go to
`outputs/LEVI/workbench/models/ollama` below the workspace. Use **Configure Qwen
connection** to save the profile, then inspect it below. Do not reuse a profile
name you need to preserve without checking its configuration.

The dedicated service uses `OLLAMA_NO_CLOUD=1`, one parallel request and one
loaded model. These settings are **not OS network isolation or a GPU lease**.
Stop only affects a process with LEVI's recorded boot/start identity, using Linux
pidfds to avoid signaling a reused PID. Active LEVI tasks/downloads block stopping
through the UI. The service can survive a frontend/core restart and be reattached;
stop it explicitly in this panel when no longer needed.

## 3. Inspect, download and bind

1. Click **Inspect local models**. This reads inventory/version/details only.
2. If absent, expand **Download model explicitly**. Review the selected model's
   license and available space; authorize the download, then start it. For Qwen,
   consult the [official model card](https://huggingface.co/Qwen/Qwen3.5-4B).
3. Progress shows actual bytes for the current layer, not a fabricated overall
   percentage. Closing the page does not cancel the persisted background job.
4. Cancel explicitly when needed. Closing LEVI's stream may not stop a download
   shared with another Ollama client. A failed/interrupted job can be retried with
   a new request; completed blobs remain in Ollama's cache.
5. Inspect again after completion. Confirm structured JSON capability, select
   image input if declared by the service, and **Bind installed model**.
6. Binding records the complete digest, template digest, version and declared
   capabilities. This is not a quality benchmark. Model or profile changes require
   a new task plan. Missing models are never pulled implicitly during execution.
7. Optional **Load bound model / Unload bound model** requires explicit hardware
   authorization. Residency is service-wide and can affect other Ollama clients.

Ollama's content-addressed internal blobs are the sole naming exception. LEVI
business outputs keep dataset names and readable run IDs. Download size is not a
prediction of VRAM use. Real hardware/quality acceptance has not been performed.

## 4. Plan, pilot, review, publish

Select the profile in a new task. Choose a small episode/camera scope and explicit
evidence-sharing consent. Approve the immutable plan, run the pilot, inspect
actual evidence, and review/edit its draft. Pilot acceptance and final commit
remain separate human actions. The local adapter sends time-labelled evidence
images plus bounded structured context; it does not assume native video input.
The existing schema and scope validators reject malformed or out-of-scope output.
There is no silent cloud fallback. Unreported usage is charged conservatively and
marked as such, not displayed as reported zero. Tokenization/visual costs are not
known in advance; reservations and post-call enforcement do not guarantee a
provider-level hard input-token cap.

## 5. Assign Codex or Claude as teacher

First create a scoped external connection using [Pilot setup](PILOT.md), for
example:

```bash
uv run levi agent connect --client codex --help
# Follow the printed project-local configuration and explicit apply flow.
```

Choose that connection under **External teacher supervision** in the new task.
Evidence-sharing consent covers the teacher as well as the learner. Both **Shadow
comparison** and **Supervised annotation** currently gate each annotation phase;
the mode is recorded for evaluation, not an automatic expansion of authority.

After the learner runs, the teacher calls `supervision.pending` with the run ID
(MCP name `supervision__pending`), retrieves real images through `evidence.read`,
and submits feedback:

```json
{
  "name": "supervision.feedback",
  "arguments": {
    "run_id": "review-20260922T1000",
    "teaching_id": "review-20260922T1000:0:coarse",
    "revision": 0,
    "decision": "accept",
    "note": "The cited frames support the suggested interval."
  }
}
```

`revise` additionally supplies a complete validated `ModelOutput` in `output`;
`reject` leaves the phase blocked. Feedback is bound to the exact phase revision
and input fingerprint. Only the assigned connection (or the human control
session) can submit it. Repeating identical feedback returns the saved result;
changing accepted feedback is a conflict. The teacher cannot bypass this gate
with another draft tool or approve the plan, pilot or publication.

After accepted feedback, **resume** the existing pilot/execution. The completed
model phase is reused from its settled cache, including when the call budget is
exhausted. A disconnected/expired teacher blocks execution. The UI follows the
existing SSE journal. Teaching records are in the same workspace SQLite store;
teacher usage stays explicitly unknown unless separately reported. No training,
skill publication, competency promotion or hidden teacher inference is automatic.

## Development validation

```bash
uv sync --locked --group dev --extra agent
mkdir -p "$LEVI_WORKSPACE/tmp/validation"
uv run pytest tests/test_ollama_protocol.py tests/test_ollama_integration.py \
  tests/test_ollama_runtime.py tests/test_execution_contracts.py \
  --basetemp="$LEVI_WORKSPACE/tmp/validation/local-model-tests"
bun run format && bun run validate && bun run build
# Optional: existing Chromium, fake HTTP responses, GPU disabled in Chromium.
export LEVI_BROWSER_TESTS=1
export LEVI_CHROMIUM_EXECUTABLE=/path/to/installed/chromium
uv run pytest tests/test_ollama_browser.py \
  --basetemp="$LEVI_WORKSPACE/tmp/validation/local-model-browser"
```

All inference/download/process tests use fakes. The browser test launches only
Next.js and mocks model APIs. Real Ollama/Qwen inference, real Codex/Claude
teaching quality, GPU contention, and an OS-isolated offline bundle remain
separate acceptance items. These commands do not verify those claims.
