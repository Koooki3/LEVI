# LEVI API

Use the frontend origin, normally `http://127.0.0.1:7860`. The runtime bridge forwards local operations to the loopback FastAPI service; byte Range and HEAD are supported. This is a single-user service, not a public multi-tenant API.

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/levi/catalog` | Local registrations (each with a `format` descriptor: kind, origin, version, fps, source, capabilities), legacy-id `aliases`, demo IDs, workspace and conversion stages |
| POST | `/api/levi/catalog` | Register `{ "path": "/workspace/dataset" }`: a LeRobot dataset directly; a raw capture returns `kind: "raw"`, `view_status: "building"` and the `view_job` building its browsing view |
| GET | `/api/levi/convert/formats` | Input and output formats, the input → output matrix and known unsupported formats with workarounds |
| POST | `/api/levi/convert/inspect` | Start an inspection job `{ "source": "captures/session-a", "options": {} }`; its `result.report` holds the detected format, summary, requirement checklist, per-episode findings and per-target compatibility |
| GET | `/api/levi/catalog/{name}` | One registered dataset with its `format` and live `revision` (404 once removed); polled by open viewers |
| DELETE | `/api/levi/catalog/{name}` | Unregister; files, annotations and reviews stay on disk |
| GET | `/api/levi/catalog/{name}/namespaces` | The dataset's namespaces ([Namespaces](WORKSPACE.md#namespaces-one-input-many-experiments)) |
| POST | `/api/levi/catalog/{name}/namespaces` | Create (or return) `{name}--{namespace}` from `{ "namespace": "r9-agentCode" }`: one more dataset over the same source, with its own annotations, runs, memory and records; 400 for an invalid name |
| GET | `/api/levi/sync` | Workspace sync status: enabled, interval/settle, last scan, pending (waiting to settle) and recent changes |
| POST | `/api/levi/sync` | Scan the workspace now; returns the changes made |
| GET | `/api/levi/samples` | DROID test-sample status: the `LEVI_DROID_SAMPLE` setting, whether a draw runs, the ledger's draws with progress, free disk space ([DROID test sample](WORKSPACE.md#droid-test-sample)) |
| POST | `/api/levi/samples/droid_raw` | Draw (or resume) a DROID raw test sample in the background `{ "size": 500, "workers": 4 }` (`size` 1–5000, `workers` 1–8); 409 while a draw runs |
| POST | `/api/levi/samples/droid_raw/cancel` | Stop the draw and close it as `cancelled` (its partial folder stays); `?discard=true` deletes the folder once the draw stopped; 409 when the draw runs in a process LEVI cannot identify |
| GET / HEAD | `/api/levi/files/{slug}/{relative_path}` | Registered dataset metadata, Parquet, images or MP4; confined to dataset root |
| GET | `/api/levi/review?repo_id=org/name` | Restore saved review |
| POST | `/api/levi/review` | Save `{ "repo_id": "…", "flagged": [0,2], "notes": "…" }` |
| GET | `/api/levi/review/export?repo_id=org/name` | Download `levi.review.v1` JSON |
| POST | `/api/levi/jobs/plan` | Preview `{ "stage": "pipeline", "source": "captures/session-a", "fps": 10, "source_fps": 30, "options": {"target": "recap_value"} }` |
| POST | `/api/levi/jobs/{id}/run` | Consume the stored plan once; body `{}` |
| GET | `/api/levi/jobs` | Latest 50 plans/jobs, structured `progress` (stages, stage, done/total, current item, elapsed, ETA, warnings) and up to 32 KB of each log tail |
| POST | `/api/levi/diagnostics` | Structural quality check `{ "repo_id": "…", "max_episodes": 0, "checks": ["metadata","temporal"], "decode_video": true }` (`max_episodes: 0` = all); the report is also kept at `outputs/LEVI/datasets/<name>/reports/quality-<YYYYmmddTHH>.json` and its `path` returned ([Data quality](QUALITY.md)) |
| GET | `/api/annotation/health` | Annotation service availability |
| POST | `/api/annotation/dataset/load` | Load `{ "repo_id": "…" }` or `{ "local_path": "/workspace/dataset" }` |
| GET | `/api/annotation/episodes/{id}/atoms?repo_id=…` | Read language atoms |
| POST | `/api/annotation/episodes/{id}/atoms` | Replace `{ "repo_id": "…", "episode_index": 0, "atoms": [...] }` |
| GET | `/api/annotation/episodes/{id}/frame_timestamps?repo_id=…` | Exact source timestamps |
| GET | `/api/annotation/episodes/outcomes?repo_id=…` | Human success/failure labels `{ "labels": { "3": {"outcome": "success", "source": "human", "updated_at": "…"} } }` |
| POST | `/api/annotation/episodes/{id}/outcome` | Set `{ "repo_id": "…", "outcome": "success" \| "failure" }` or clear with `"outcome": null` |
| POST | `/api/annotation/export` | New annotated tree (`<name>_annotated/`, updated in place on re-export); optional `output_dir`, `copy_videos`; refused (409) for a raw capture's browsing view |
| POST | `/api/annotation/push_to_hub` | Explicit export and upload using the upstream backend implementation |
| GET | /api/annotation/sam3/status | Global SAM3 model/account/checkpoint/download status; no Torch import or CUDA probe |
| GET | /api/annotation/sam3/capabilities | Compatibility alias for the status report |
| POST | `/api/annotation/sam3/plan` | Validate and stage a model-neutral object annotation plan |
| POST | /api/annotation/sam3/run | Queue the globally available provider=sam3 worker; provider=fake remains CPU-only test support |
| GET | `/api/annotation/sam3/jobs/{id}` | Poll a worker job and publish its validated sidecar revision |
| POST | `/api/annotation/sam3/jobs/{id}/cancel` | Cancel a queued/running SAM3 worker |
| GET | `/api/annotation/sam3/revisions` | List object annotation revisions |
| GET | `/api/annotation/sam3/episodes/{id}/objects` | Read object masks/bboxes, with `camera_key`, `frame_index` and `annotation_revision` filters |
| POST | `/api/annotation/sam3/edits` | Revision-checked accept/reject/relabel/occlusion/delete/refine |

For the `local/<slug>` repo IDs returned by registration, the annotation API resolves the registered directory automatically. Direct `local_path` also works if it remains inside the configured workspace.

### Annotation example

```json
{
  "repo_id": "local/registered-id",
  "episode_index": 0,
  "atoms": [
    {"role":"user","content":"Reach for the strawberry","style":"subtask","timestamp":0.0},
    {"role":"user","content":"Pause here","style":"interjection","timestamp":1.2}
  ]
}
```

The upstream VQA answer JSON and `tool_calls` structures are preserved; use the UI to create grounded annotations correctly. Invalid styles and atoms are rejected by the annotation validator.

### Explicit Hub upload

`POST /api/annotation/push_to_hub` accepts `repo_id` or `local_path`, `hf_token`, `push_in_place`, `new_repo_id`, `private`, `commit_message`. Prefer a new target repository and set `push_in_place=false`. A write-capable token is required. No upload is performed by ordinary browsing, diagnostics, annotation saves or conversion. Never include a real token in a committed example or terminal log.

### Error handling

- `400`: invalid source, stage, check name or workspace boundary.
- `401`: the Hugging Face session is missing or cannot read the configured SAM3 model.
- `403`: disallowed asset or browser cross-origin write.
- `404`: unknown plan, missing file or dataset metadata.
- `409`: output already exists, or export would overlap the source.
- `413`: frontend bridge request body exceeds 16 MiB.
- `422`: typed request validation failed.
- `502`: local backend is unavailable; start both services with the uv launcher.
- `503`: SAM3 is disabled or its worker configuration is unavailable.

Diagnostic calls are synchronous and can take time to download remote shards. Conversion calls return a background job. Concurrent editing of the same dataset by multiple users is outside the single-user model; deploy separate workspaces/processes when isolation is needed.

### Built-in conversion plans

`catalog` reports `conversion_engine=levi.builtin.v1`, `conversion_available` (ffmpeg/ffprobe), and all bundled stages. `JobPlan.options` uses the strict schema documented in [CONVERSION.md](CONVERSION.md); unknown keys and invalid values are rejected. Top-level FPS fields override FPS entries in options. For `stage: "pipeline"`, `options.target` selects the export (`lerobot_v21` default, `recap_value`) and `options.target_options` its settings (validated at plan time); the target's defaults (RECAP: `timing: "retime"`, `filter_static: false`) apply to options the request does not set. Human outcome labels of the source are snapshotted into `options.outcome_labels`. A plan for an input/target pair the registry does not support is rejected.

Plans capture source size/mtime fingerprints. Execution fails if the source changes before or during processing. The worker is always `python -m levi.conversion`. Job results include `exit_code`, structured `result` (with `video_modes`, `fps`, optional `fps_note`), `output_exists`, the automatically registered `dataset` and, when the source had annotations, a `carryover` report. The output directory is the dataset itself; the default name is `<source>_<lerobot|recap>_<timestamp>`, and a job id is the same timestamp. Stage `view` builds a raw capture's browsing view and updates its catalog entry.

Read-only stages (`inspect`, `summary`, …) have no output dataset. A failing report exits with code 2 and is recorded as failed; exceptions exit nonzero. Nothing is published unless the whole run succeeds. Interrupted jobs are marked on service restart and never resumed automatically.

## Service entry points / 服务入口

The browser workbench is served on `http://127.0.0.1:7860`. Backend port `7861` serves the API: `/` returns a bilingual entry guide, `/favicon.ico` returns the LEVI icon, and `GET /api/levi/health` returns `{"service":"levi-api","status":"ok"}` for startup checks. The launcher supplies the guide with the selected frontend address/port. These entry routes do not expose datasets or credentials.

网页入口为 7860；7861 是内部 API。完整启动使用 `uv run levi` 或 `uv run levi serve`。远程访问时，应把本机网页端口转发到服务器的网页端口。

### SAM3 object annotation

Object annotations are stored outside the source dataset. A plan uses `episode_indices`, `camera_keys`, `prompts`, optional `start_frame`/`max_frames`, review thresholds and `provider` (`fake` or `sam3`). The fake provider is deterministic and CPU-only. Real provider requests return `202` with a `job_id`; poll it until `succeeded`, then use the returned `revision_id`. The UI sends the `plan_id` returned by `/plan` to `/run`, so execution is bound to the exact preflighted plan; changing any plan field requires a new preflight.

Read rows with `annotation_revision=<id>`; the dataset `revision` query parameter remains reserved for the source dataset revision. Send `base_revision` in edits so a stale browser cannot overwrite a newer review. See [SAM3.md](SAM3.md) for the sidecar schema, global environment and ordered deployment sequence.

## SAM3 runtime status

The global status route reports the configured model mirror (1038lab/sam3,
sam3.pt, main), current Hugging Face account identity, workspace checkpoint path
and download progress. `POST /api/annotation/sam3/checkpoint/download` uses the
request's browser, CLI-cache or `HF_TOKEN` credential, starts one resumable
background download and returns the same status shape. It never returns a token
or imports Torch/probes CUDA. The real worker receives the active credential only
for its child process; the local hf CLI cache or HF_TOKEN can be used as a fallback.

Object annotation requests accept either a Hub repo_id or a registered local
dataset. Hub state and sidecars are scoped by a one-way credential digest, while
local datasets are scoped by their resolved path and LEVI_WORKSPACE. This prevents
account changes from reusing another account's private snapshot or revision.
See SAM3.md for the ordered setup sequence and sidecar schema.

## Agent capabilities

All agent routes live under `/api/levi/agent/v1`. `GET /capabilities` lists every capability with its input schema, permission and side effects; `POST /tools` runs one: `{ "name": "runs.list", "arguments": {…}, "idempotency_key": "…" }`. The same registry backs the web UI, the stdio MCP bridge (tool names use `__` for `.`) and the `levi agent` CLI, so authorization and audit are identical on every channel.

An external agent authenticates with its scoped connection credential (`Authorization: Bearer …`; see `levi agent connect`) or the legacy `LEVI_AGENT_TOKEN` / `LEVI_AGENT_DATASETS` pair. It may read and draft on its datasets only; plan approval, pilot review, commit, reset, clean, model management and publishing improvements require the operator session. After agent revisions are active, legacy annotation/review writes must send the `X-LEVI-Annotation-Revision` returned by their read.

Capability groups — orientation, quality, planning, execution, evidence, annotations, objects, natural-language tasks, harness (memory, cost, improvements), supervision, workspace — and who may call each are listed in [Agents → Capability reference](AGENTS.md#capability-reference). Live activity streams as Server-Sent Events at `/activity/stream` (operator only).

### Capability reference

Generated from the capability registry by `uv run levi docs sync`. Do not edit by hand.

<!-- levi:generated capabilities -->
| Capability | Who | What it does |
| --- | --- | --- |
| `annotations.propose_events` | agent | Stage event suggestions using the same validators |
| `annotations.propose_segments` | agent | Stage evidence-grounded suggestions; never approve |
| `capabilities.list` | agent | Discover schemas, scope and side effects |
| `changes.approve` | **person** | Human approval of an exact draft revision |
| `changes.commit` | **person** | Atomically publish all approved annotation changes |
| `changes.diff` | agent | Read typed suggestions, base version and provenance |
| `changes.edit` | agent | Replace draft proposals with a revision precondition |
| `changes.rebase` | **person** | Move a staged draft onto the current published revision; clears approval |
| `changes.review` | **person** | Accept or reject selected suggestions in one human action |
| `changes.undo` | agent | Create a conflict-checked inverse draft requiring human review |
| `changes.validate` | agent | Validate source, evidence, range and annotation revision |
| `cost.profile` | agent | Measured token and time cost on this dataset per agent (API, local VLM, external MCP), the latest breakdown and advice for the next run |
| `datasets.inspect` | agent | Inspect fixed dataset scope and snapshot cost |
| `episodes.query` | agent | Read frozen episode scope |
| `evidence.boundaries` | agent | Check staged segments: one sheet row per boundary (frames from 1 s before to 1 s after it) with the plan's start and end definitions of the subtasks it separates |
| `evidence.changes` | agent | Rank an episode's coarse intervals by how much the picture changes; refine the top ones first |
| `evidence.read` | agent | Read a bounded page of exact evidence; layout='mosaic' returns one labelled sheet instead of one image per frame |
| `evidence.refine` | agent | Add bounded extra frames around candidate boundaries, within the approved window and frame cap |
| `export.plan` | agent | Describe native export constraints; never upload |
| `export.run` | **person** | Export reviewed native data to a new directory and re-read lossless sidecars |
| `gpu.status` | agent | What the GPU guardian decides now for local models, why, the expected wait, and the window it learned for each workload |
| `improvements.evaluate` | agent | Let LEVI measure a candidate on stored evidence at named probe cases |
| `improvements.get` | agent | Read one improvement candidate |
| `improvements.list` | agent | List harness improvement candidates for a dataset and the parameters a new task would run with |
| `improvements.revise` | agent | Change a candidate's value once during evaluation; it must then pass a new evaluation |
| `improvements.transition` | agent | Move a candidate through the state machine; publishing, retaining and rolling back need a person |
| `knowledge.list` | agent | Built-in knowledge entries, and local notes that could join them |
| `knowledge.promote` | **person** | Add a local note to built-in knowledge (repository file); a person's call |
| `knowledge.reject` | **person** | Decline a knowledge candidate; a person's call |
| `media.sample` | agent | Read frozen sampled evidence and coverage |
| `memory.get` | agent | Read this dataset's verified local memory: committed annotations, recurring uncertainty, cost and published lessons |
| `memory.rebuild` | **person** | Recompute a dataset's memory from its closed runs; a person's call |
| `memory.search` | agent | Search committed segments and lessons in the dataset's local memory |
| `objects.detect` | agent | Measure candidate object regions in evidence frames; no model, no GPU |
| `objects.edit` | **person** | Human correction of staged tracks; invalidates approval |
| `objects.frame` | agent | Read persistent staged masks for the existing player clock |
| `objects.get` | agent | Get staged SAM3 progress |
| `objects.inspect` | agent | Review staged masks against immutable source frames |
| `objects.plan` | agent | Plan SAM3 against the frozen snapshot |
| `objects.propose` | agent | Stage agent-authored object outlines for the same human review as worker output |
| `objects.run` | agent / operator | Run isolated SAM3 worker; stage results for human review |
| `objects.status` | agent | Check configured worker/checkpoint without probing CUDA |
| `objects.strategy` | agent | Recommend SAM3 or agent-authored object annotation for this machine and scope |
| `plans.approve` | **person** | Approve the exact execution contract; does not approve annotation commit |
| `plans.clarify` | agent | Ask only missing requirements; never calls a model |
| `plans.estimate` | agent | Estimate the agent tokens a scope will cost, from recorded runs of this agent |
| `plans.rebudget` | **person** | Revise budget, retain completed shards, revoke execution approval |
| `plans.review_pilot` | **person** | Accept or reject pilot quality before expanding scope |
| `quality.inspect` | agent | Check a dataset's structure, timing, media, actions and distribution; writes <dataset>/reports/quality-<hour>.json |
| `runs.abandon` | **person** | Close a run nobody will finish, so its evidence can be cleaned up |
| `runs.cancel` | agent / operator | Request cancellation; not an immediate termination claim |
| `runs.events` | agent | Replay sequenced run events |
| `runs.execute` | agent / operator | Execute pilot or remaining shards; consumes model budget |
| `runs.finish` | agent | Freeze completed shards for partial human review without another model call |
| `runs.get` | agent | Read a durable run |
| `runs.list` | agent | List runs in scope and what each one is waiting for |
| `runs.pause` | agent / operator | Request pause at next safe boundary |
| `runs.plan` | agent | Persist an immutable run plan; no model call |
| `runs.prepare` | agent | Create bounded immutable evidence artifacts without a model call |
| `runs.quality` | agent | Read the run's annotation uncertainty and uncovered intervals per episode (not dataset quality: that is quality.inspect) |
| `runs.report_usage` | agent | Report this agent's own token/request use for the run; feeds cost estimates |
| `runs.result` | agent | Read the durable artifact manifest and review status |
| `runs.resume` | agent / operator | Resume uncompleted shards without repeating completed ones |
| `supervision.feedback` | agent | Accept, revise or reject a learner phase; never approve execution or commit |
| `supervision.pending` | agent | Read the assigned teacher's evidence and pending annotation phases |
| `tasks.advance` | agent / operator | Run the task's next automatic step; stops at every human gate |
| `tasks.approve` | **person** | Human approval of a checked task spec |
| `tasks.feedback` | agent | Accept, correct or reject an interpretation; kept as a lesson |
| `tasks.get` | agent | Read a natural-language task and its steps |
| `tasks.interpret` | agent | Turn a natural-language request into a checked task spec with the local model; nothing runs until a person approves it |
| `view.seek` | agent | Return evidence navigation; never force browser navigation |
| `workspace.clean` | **person** | Remove regenerable run snapshots and evidence; keeps committed revisions |
| `workspace.get_context` | agent | List accessible catalog IDs, never local paths |
| `workspace.reset` | **person** | Remove one dataset's agent history: runs, records and published revisions |
<!-- /levi:generated capabilities -->

## Local models (Ollama)

Model management requires the operator session; an external agent cannot approve its own download or start request.

| Method / path | Behavior |
| --- | --- |
| `POST /providers` | `kind: ollama`, loopback `base_url`, model and explicit `allow_localhost`; no key |
| `GET /providers/{name}/ollama` | Inspect service-declared metadata; no inference |
| `POST /providers/{name}/ollama/bind` | Bind the installed digest, explicit `structured_output: true`, optional vision |
| `POST /providers/{name}/ollama/download` | `approve_download: true`, unique `request_id`; returns a persisted job, HTTP 202 |
| `GET /model-downloads[/{id}]` | Persisted download progress |
| `POST /model-downloads/{id}/cancel` | Stop consuming the download stream |
| `POST /providers/{name}/ollama/memory` | `operation: load/unload`, `approve_hardware_use: true` |
| `GET /ollama/runtime` | LEVI-owned process state and managed paths |
| `POST /ollama/runtime/start` | `port`, `approve_start: true`; refused while another process uses the GPU (off-peak guard) |
| `POST /ollama/runtime/stop` | Stop the recorded owned process |

`TaskContext` accepts `supervision: none | shadow | supervised` and `teacher_grant`; `supervision.pending` / `supervision.feedback` are ordinary capabilities restricted to the assigned teacher or the operator. See [Local models](OLLAMA.md).
