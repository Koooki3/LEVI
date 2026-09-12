# LEVI API

Use the frontend origin, normally `http://127.0.0.1:7860`. The runtime bridge forwards local operations to the loopback FastAPI service; byte Range and HEAD are supported. This is a single-user service, not a public multi-tenant API.

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/levi/catalog` | Local registrations, demo IDs, workspace and available conversion stages |
| POST | `/api/levi/catalog` | Register `{ "path": "/workspace/dataset" }` |
| GET / HEAD | `/api/levi/files/{slug}/{relative_path}` | Registered dataset metadata, Parquet, images or MP4; confined to dataset root |
| GET | `/api/levi/review?repo_id=org/name` | Restore saved review |
| POST | `/api/levi/review` | Save `{ "repo_id": "…", "flagged": [0,2], "notes": "…" }` |
| GET | `/api/levi/review/export?repo_id=org/name` | Download `levi.review.v1` JSON |
| POST | `/api/levi/jobs/plan` | Preview `{ "stage": "pipeline", "source": "captures/session-a", "fps": 10, "source_fps": 30, "options": {} }` |
| POST | `/api/levi/jobs/{id}/run` | Consume the stored plan once; body `{}` |
| GET | `/api/levi/jobs` | Latest 50 plans/jobs and up to 32 KB of each log tail |
| POST | `/api/levi/diagnostics` | Diagnose `{ "repo_id": "…", "max_episodes": 20, "checks": ["metadata","temporal"], "decode_video": false }` |
| GET | `/api/annotation/health` | Annotation service availability |
| POST | `/api/annotation/dataset/load` | Load `{ "repo_id": "…" }` or `{ "local_path": "/workspace/dataset" }` |
| GET | `/api/annotation/episodes/{id}/atoms?repo_id=…` | Read language atoms |
| POST | `/api/annotation/episodes/{id}/atoms` | Replace `{ "repo_id": "…", "episode_index": 0, "atoms": [...] }` |
| GET | `/api/annotation/episodes/{id}/frame_timestamps?repo_id=…` | Exact source timestamps |
| POST | `/api/annotation/export` | New annotated tree; optional `output_dir`, `copy_videos` |
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

`catalog` reports `conversion_engine=levi.builtin.v1`, `conversion_available` (ffmpeg/ffprobe), and all bundled stages. `JobPlan.options` uses the strict schema documented in [CONVERSION.md](CONVERSION.md); unknown keys and invalid values are rejected. Top-level FPS fields override FPS entries in options.

Plans capture source size/mtime fingerprints. Execution fails if the source changes before or during processing. The worker is always `python -m levi.conversion`; old external-script plans cannot run. Job results include `exit_code`, structured `result`, `output_exists`, and an automatically registered `dataset` when successful. `pipeline` updates the job output to its nested validated `dataset/` directory. Intermediate captures remain in the run directory.

Read-only stages have no output dataset. A failing report exits with code 2 and is recorded as failed; exceptions exit nonzero. Interrupted jobs are marked on service restart; partial output is retained, never resumed automatically.

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
