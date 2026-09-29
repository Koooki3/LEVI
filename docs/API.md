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
| GET | `/api/levi/pool/status`, `/api/levi/pool/sources`, `/api/levi/pool/tasks`, `/api/levi/pool/episodes` | Training pool: settings and last scan, sources, per-task counts, the episode index (filters and paging in [Training pool](TRAINING_POOL.md#api)) |
| POST | `/api/levi/pool/scan` | Start a scan job of `LEVI_POOL_ROOTS`; poll `GET /api/levi/pool/jobs/{id}` |
| GET / PUT / DELETE | `/api/levi/pool/recipes/{name}` | Saved training-pool recipes (`GET /api/levi/pool/recipes` lists them) |
| POST | `/api/levi/pool/preview` | Counts and exclusions of a recipe `{ "recipe": {…}, "format": "lerobot_v21" }` |
| POST | `/api/levi/pool/export` | Plan (`dry_run`) or start an export `{ "recipe_name": "…", "options": {"format": "lerobot_v21", "name": "…"} }`; held-out episodes are refused, 403 for a folder outside `LEVI_EXPORT_ROOTS` or inside a source |
| GET | `/api/levi/pool/facets` | Training pool facet counts and what the visibility toggles hide |
| POST | `/api/levi/pool/jobs/{id}/cancel` | Stop a running pool scan, export or push |
| GET | `/api/levi/pool/jobs/{id}/summary` | `pool_export.json` of a finished export job |
| GET | `/api/levi/pool/remotes` | Remote targets for pool pushes |
| PUT / DELETE | `/api/levi/pool/remotes/{name}` | Register `{ "spec": "[user@]host:/path", "port"?: n }` or forget a target (SSH keys only; a `password` field is refused) |
| POST | `/api/levi/pool/push` | `{ "target": "…", "export_job": "…" \| "source": "<export dir>", "dry_run": false }`: rsync over SSH of a finished pool export, as a cancellable job |
| GET | `/api/levi/manifest/operations` | Built-in training-manifest operations with their parameters ([Training manifests](TRAINING_MANIFEST.md)) |
| GET | `/api/levi/manifest?repo_id=local/<name>` | Manifests written for a dataset: directory, time, operation, counts |
| POST | `/api/levi/manifest` | Write a manifest `{ "repo_id": "local/<name>", "operation": "verified_success", "params": {"fallback": "exclude"}, "tasks": ["…"], "episodes": [0, 1], "anchored_run": null, "anchored_tasks": ["…"], "recap_revision": null, "allow_stale": false }`; returns the manifest without its per-episode table (that stays in `manifest.json`); 400 for an unknown operation or parameter, a missing anchored review or RECAP revision, or stale RECAP labels |
| GET | `/api/levi/report?lang=en\|zh` | The technical report ([below](#technical-report--技术报告)): `{configured, dir, exists, lang, document_lang, markdown, status, errors, mtime, etag}`; answers `304` to a matching `If-None-Match` |
| GET | `/api/levi/report/version?lang=en\|zh` | The report's change marker `{configured, etag, mtime}` from file stats only; the page polls it every 5 s |
| GET / HEAD | `/api/levi/report/assets/{relative_path}` | An image (PNG, JPEG, GIF, WebP, SVG) under the report's `assets/`; anything else, a hidden name or a path leaving the folder is `403` |
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
| GET | `/api/annotation/segmentation/status` | Fast segmentation: worker and teacher readiness, student models, cameras, recent jobs, live sessions ([Fast segmentation](SEGMENTATION.md)) |
| GET/DELETE | `/api/annotation/segmentation/models[/{name}]` | List or delete distilled student models |
| POST | `/api/annotation/segmentation/label` | Label episodes (all by default) with a student; `202` job |
| POST | `/api/annotation/segmentation/distil` | Distil a student from SAM3 pseudo-labels; `202` job |
| GET/POST | `/api/annotation/segmentation/jobs/{id}[/cancel]` | Poll (publishes a finished labelling job) or cancel |
| POST | `/api/annotation/segmentation/live` | Start a live overlay session for one episode (answers once the model is loaded) |
| POST | `/api/annotation/segmentation/live/{id}/clock` | Player clock `{playing, time, rate}` |
| GET | `/api/annotation/segmentation/live/{id}/events` | Server-sent events: per-camera `result`, `stats`, `stopped`, `error`, `closed` |
| POST | `/api/annotation/segmentation/live/{id}/stop` | Stop and save the shown frames for this episode only |

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

## Technical report / 技术报告

The **Report / 报告** page (`/report`) renders a technical report kept outside LEVI: set `LEVI_REPORT_DIR` to its folder (for example in `.env`). LEVI only reads that one folder; it may lie outside `LEVI_WORKSPACE`, and nothing else outside the workspace becomes readable. Unset, or pointing at a missing folder, the page explains how to configure it.

| File | Content |
| --- | --- |
| `LEVI.md`, `LEVI.zh-CN.md` | The report in English and Chinese (GitHub-flavoured Markdown; raw HTML is not rendered). The page follows the language switch and falls back to English. |
| `status.json` | Live data, schema `levi.report.status.v1`: `generated_at`, `levi_main`, `workstreams` (state, progress 0–1, stage, ETA), `metrics`, `charts`, `tables`, `milestones`, `resources` (GPU, free disk). Bilingual fields are `{"en": "…", "zh": "…"}`. |
| `assets/` | Images the Markdown references as `assets/<file>`. |

Fenced blocks with a JSON body become components: `levi-progress` (`{"source": "workstreams"}` or `{"id": "W1b"}`), `levi-chart` (`{"type": "bar"|"line"|"grouped-bar", "title", "data": "<charts key>" or inline rows, "x", "series": [{"key", "label"}], "y_label", "y_domain"}`), `levi-metrics`, `levi-table` and `levi-timeline` (`{"data": "<key>"}` in `metrics`, `tables`, `milestones`). A block that does not parse shows its error in place; the rest of the report still renders. The page polls `/api/levi/report/version` every 5 s and swaps in a changed report without moving the reader's scroll position.

技术报告页读取 `LEVI_REPORT_DIR` 指向的目录（只读，可在工作区之外）：`LEVI.md` / `LEVI.zh-CN.md` 为正文，`status.json` 为实时数据，`assets/` 为图片。页面随语言切换选择中文或英文（缺失时回退英文），每 5 秒检测一次变化并无闪烁地更新。

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
| `anchored.get` | agent | Results of an anchored review: per-episode outcomes, or one episode's events with their answers, validity and cited frames, its start check and veto verdicts, and what the outcome rests on (including undecided labels, vetoes and contested waivers) |
| `anchored.specs` | agent | Built-in anchored review specs: the event each is anchored on, the frames per camera, the question, answer fields and success rules, any start check and vetoes, and a status (stable, or candidate: still being validated, never a default) |
| `annotations.propose_events` | agent | Stage event suggestions using the same validators |
| `annotations.propose_segments` | agent | Stage evidence-grounded suggestions; never approve. new_subtasks adds a subtask the vocabulary lacks (never an existing id or alias) |
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
| `recap.get` | agent | Current RECAP advantage labels: per-episode positive fraction and mean value, or for one episode its runs of positive/negative frames and V(o_t) about once a second |
| `recap.status` | agent | RECAP value model on this dataset: checkpoints and their readiness, the worker, the current advantage labels (threshold, stale) and the latest job; reads only |
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
| `segmentation.status` | agent | Fast instance segmentation on this dataset: the distilled student models (concepts, held-out scores against the SAM3 teacher, licences), the worker's readiness and the latest labelling or distillation jobs; reads only |
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

## Local models (Ollama, local OpenAI-compatible servers)

Model management requires the operator session; an external agent cannot approve its own download or start request. The inspect and bind endpoints also serve `kind: openai-local` profiles (a local vLLM server, see [Local models](OLLAMA.md#8-a-local-openai-compatible-server-vllm)); download and memory requests for them return HTTP 409, since the server loads and keeps its own weights.

| Method / path | Behavior |
| --- | --- |
| `POST /providers` | `kind: ollama` or `openai-local`, loopback `base_url` (the server root), model and explicit `allow_localhost`; no key (`openai-local` sends one only when `key_env` is set) |
| `GET /providers/{name}/ollama` | Inspect service-declared metadata; no inference |
| `POST /providers/{name}/ollama/bind` | Bind the installed digest, explicit `structured_output: true`, optional vision; `openai-local` also sets `context_tokens` to the server's context (refused above 131072) |
| `POST /providers/{name}/ollama/download` | `approve_download: true`, unique `request_id`; returns a persisted job, HTTP 202 |
| `GET /model-downloads[/{id}]` | Persisted download progress |
| `POST /model-downloads/{id}/cancel` | Stop consuming the download stream |
| `POST /providers/{name}/ollama/memory` | `operation: load/unload`, `approve_hardware_use: true` |
| `GET /ollama/runtime` | LEVI-owned process state and managed paths |
| `POST /ollama/runtime/start` | `port`, `approve_start: true`; refused while another process uses the GPU (off-peak guard) |
| `POST /ollama/runtime/stop` | Stop the recorded owned process |

`TaskContext` accepts `supervision: none | shadow | supervised` and `teacher_grant`; `supervision.pending` / `supervision.feedback` are ordinary capabilities restricted to the assigned teacher or the operator. See [Local models](OLLAMA.md).
