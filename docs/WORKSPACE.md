# Workspace layout

What lives under `LEVI_WORKSPACE` (default `.state/` at the repo root), how it's named, what's safe to delete, and how the two different "converted dataset" concepts relate. Read alongside [Conversion](CONVERSION.md) (raw-capture conversion) and [SAM3](SAM3.md) (object-annotation sidecar).

## Layout

All paths are relative to `LEVI_WORKSPACE` (`levi/paths.py`).

| Path | Contents | Cleanup |
| --- | --- | --- |
| `<dataset-name>/` | Any registered dataset — a raw capture tree, or a LeRobot dataset — flat, directly under the workspace root. | **Protected.** `levi clean` never touches it. |
| `<source>_<lerobot\|recap>_<timestamp>/` | A conversion's default output (when no custom output is given). The directory **is** the dataset (`meta/`, `data/`, `videos/`); its reports are `meta/levi_preflight.json`, `meta/levi_validation.json`, `meta/levi_annotation_carryover.json`. Written to a hidden `.<name>.partial` sibling and renamed into place only on success. | **Protected**, not auto-pruned. |
| `outputs/LEVI/workbench/` | LEVI's own state: `datasets.json` (catalog, keyed by dataset name) + `dataset_aliases.json` (legacy hash ids → names), `annotations/<name>/` (language/event per episode, plus `outcomes/`, `status/` (episodes a person confirmed complete) and `vocabulary.json` (the dataset's subtask vocabulary)), `object_annotations/<name>/` (SAM3 revisions), `reviews/<name>.json`, `jobs/<timestamp>.*` (plan, log, options, result, progress; `jobs/<timestamp>/intermediate/` when `keep_intermediates`), `views/<name>/` (browsing views of raw captures). | Protected (views are rebuilt on demand). |
| `droid_raw_<size>_draw<NN>/` | A DROID raw test sample (see [DROID test sample](#droid-test-sample)): `demo_0000/` … plus `_meta/` (`selected.tsv`, `selection.json`, `verification.json`). Assembled in a hidden `.<name>.partial` sibling and renamed into place only when every episode is verified. | **Protected**, like any dataset. |
| `outputs/LEVI/workbench/samples/` | The sample ledger `droid_raw.json` (draws, positions used in the release order, planned bytes), the release listing `droid_raw/episodes.txt`, and per draw `<name>.progress.json` and `<name>.log`. | Protected. |
| `outputs/LEVI/exports/` | Annotated-dataset exports, `<name>_annotated/`; re-exporting the same source updates it in place. | Protected. |
| `checkpoints/` | Model weights: `sam3/` (SAM3 checkpoint), `ollama/` (local models, Ollama's own content-addressed blob store — the one place a digest names a file, by the engine's design). | Protected. |
| `eval/` | Annotation evaluation records: `<dataset>_human_<YYYYmmddTHH>.md` (a person's recorded session) and `<dataset>_agent_<YYYYmmddTHH>_<driver>.md` (an agent's full-dataset subtask annotation); `eval/.sessions/` holds recordings in progress. See [Evaluation records](EVALUATION.md). | Protected. |
| `memory/` | Optional local notes for LEVI development (issue register, data and harness lessons). LEVI never reads it into a prompt; knowledge that holds for any dataset is promoted to `levi/knowledge/` instead. | Protected. |
| `.cache/`, `tmp/` | HF Hub cache (`org__name/account-0001/revision-0001/`, with revision/account fingerprints in `cache-index.json`), build/test temp files. | `levi clean` covers this. |

### Naming convention (no hash suffixes)

- **One per dataset, overwritten on re-run** — annotation sidecars, SAM3 store, outcome labels, reviews, exports: the dataset's **catalog name** (`levi/naming.py`, `levi/catalog.py`). The catalog guarantees uniqueness: a second dataset with the same folder name gets `<name>_<timestamp>`; a generic folder (`dataset`, `data`, `output(s)`) is named after its parent. Several LEVI processes resolve the same dataset to the same directory, so collaborators' edits land in the same files.
- **One per run** — jobs, conversion outputs, SAM3 plans/jobs/revisions, a clashing export: a timestamp `YYYYmmdd-HHMM`, reserved atomically (`O_EXCL` / `mkdir`) so concurrent processes never collide (`-1`, `-2`… within the same minute). Agent runs and revisions use `<kind>-YYYYmmddTHHMM` (`-2`… on a clash) under the dataset's own directory, so a path reads as dataset, kind of work and minute. Precision stops at the minute deliberately: finer digits are an opaque suffix. All stamps are UTC.
- **Review and analysis reports** — an hour stamp on the dataset's own report: `outputs/LEVI/datasets/<name>/reports/quality-<YYYYmmddTHH>.json` (Doctor and MCP `quality.inspect` write the same file; a re-run in the same hour replaces it). Browser downloads follow the same rule: `<name>-quality-<YYYYmmddTHH>.json`, `<name>-review-<YYYYmmddTHH>.json`.
- Content/account fingerprints are stored in metadata, never used as LEVI artifact path suffixes. New Hub caches allocate readable account/revision slots under the dataset name; old caches remain untouched. Atomic temporary files use timestamps. Third-party package managers own their internal cache layout.
- Legacy hash ids still resolve (`dataset_aliases.json`); old `/local/<hash>` URLs redirect to the name and carry the browser's flagged episodes over. `uv run levi migrate` (dry run; `--apply` with the service stopped) moves older state to this scheme: re-keys the catalog, re-homes the old `<run>/dataset` layout (dataset up one level, reports into `meta/`, `capture/`/`filtered/` into `jobs/<timestamp>/intermediate/`), renames reviews, diagnostics, job records and hash-suffixed exports. Applied to this workspace on 2026-09-18.

### Agent run artifacts

Evidence images and contact sheets are named after the episode and content they hold — `episode_000000--<camera>--frame_000040.png`, `episode_000000--sheet-0000-008-w320.png`, `…--candidates.png` — never after a digest. Committed annotation revisions are the run's UTC timestamp; the object sidecar inside them keeps its own `YYYYmmdd-HHMMSS-fff` revision. `levi agent clean` deletes the regenerable half of this (input snapshots, evidence, sheets) for runs that have ended, and keeps revisions, provenance, inverse patches, the evidence ledger and open drafts.

### Per-dataset outputs, local memory and improvement candidates (`levi/harness/`)

Every agent run that ends (committed, cancelled, failed) is closed exactly once, without a model call:

| Path | What |
|---|---|
| `outputs/LEVI/datasets/<name>/tasks/<run>/ledger.json` | The run's facts: scope, per-tool calls/time/response bytes for the agent (`tools`) and for the web UI (`human_calls`), tokens LEVI delivered to the agent (measured) next to the agent's own report, quality warnings per `episode_NNNNNN`, the published revision. Rewritten, never duplicated, when a late usage report arrives. |
| `outputs/LEVI/datasets/<name>/tasks/task-<timestamp>/task.json` | A natural-language task: the request, the checked spec, each step's state and run, the final token/time report. |
| `outputs/LEVI/datasets/<name>/reports/quality-<YYYYmmddTHH>.json` | Structural quality report, one per hour; a re-run in the same hour replaces it. |
| `outputs/LEVI/datasets/<name>/teaching/` | Teacher feedback per phase (`<run>/episode_NNNNNN-<phase>.json`), frozen references (`reference-<task>.json`) and the learner's grades (`grades.json`). |
| `outputs/LEVI/workbench/knowledge/candidates.json` | Local notes that could become built-in knowledge (`levi/knowledge/*.md`), refreshed at every run closure; a person promotes or rejects them (`levi agent knowledge`, see [Built-in knowledge](KNOWLEDGE.md)). |
| `outputs/LEVI/workbench/memory/workspace.json` | Notes about using LEVI itself (e.g. how to read a request), shared by every dataset. |
| `outputs/LEVI/workbench/models/ollama/` | The LEVI-owned Ollama process record, its log, and the GPU guard's last-busy record. |
| `outputs/LEVI/workbench/memory/<name>.json` | The dataset's local memory. Cost part (every closed run, measured): `cost_profiles` per agent key (API model, local VLM, external MCP) with medians of tokens and seconds per episode, `cost_latest`, `cost_hints`. Verified part: only human-committed segments, per `episode_NNNNNN` with the run and revision they came from; subtask/outcome statistics, recurring uncertainty and failure reasons, cost per episode, lessons from published improvements. Handed to the next task at plan time (`run.harness.memory`). `levi agent memory show|search|rebuild`. |
| `outputs/LEVI/workbench/improvements/<name>/<slug>.json` | One file per improvement candidate, named for what it changes (e.g. `refine-at-coarse-change.json`). Harness candidates move proposed → evaluating → qualified → awaiting_authorization → published → observing → retained/rolled_back; software candidates end resolved/rejected. Publishing, retaining, rolling back and resolving need a person: `levi agent improvements list|show|publish|reject|retain|rollback|resolve`. A run keeps the harness parameters it was planned with (`run.harness`, covered by the plan digest). |

## Agent runs and committed annotation versions

After the first Agent commit, the active annotation bundle is selected by SQLite (`outputs/LEVI/workbench/agent/workbench.sqlite3`). Runs and immutable versions live at `agent/datasets/<dataset-name>/runs/<UTC-timestamp>/` and `agent/datasets/<dataset-name>/revisions/<UTC-timestamp>/`. They contain evidence, bounded input copies, staged SAM3 results, language/outcome/object sidecars and review decisions. Original datasets are never changed. Legacy editors, exports and conversion carry-over use the same active version resolver. Back up the complete workbench tree with services stopped; do not move only the SQLite file. See [Agents](AGENTS.md).

## Runtime sync

LEVI follows the workspace while it runs (`levi/sync.py`, a background scan every `LEVI_SYNC_INTERVAL` s, default 5; `POST /api/levi/sync` or **Sync now** in the Workbench scans at once):

| On disk | What LEVI does |
| --- | --- |
| New LeRobot dataset or raw capture, up to 3 levels under the workspace (not `outputs/`, `checkpoints/`, `tmp/`, hidden folders or `.partial` staging) | Registered once it has stopped changing for `LEVI_SYNC_SETTLE` s (default 10); a raw capture's view is built. `LEVI_SYNC_DISCOVER=lerobot` limits this to LeRobot datasets, `off` disables it. |
| Episodes added, removed or rewritten in a LeRobot dataset | Catalog info and revision refreshed; the annotation backend drops its cached episode table; an open viewer offers a reload. |
| Demos added, removed or modified in a raw capture | View rebuilt once the capture is stable; annotations and outcome labels re-keyed by demo (vanished demos to `orphaned/`); open viewers offer a reload. A capture whose view failed is retried only after it changes. |
| Dataset folder deleted or moved away | Entry removed (a raw capture's generated view too). Annotations, labels, reviews and SAM3 revisions stay under the name and re-attach if a dataset with that name returns. A dataset moved to a new folder name is a new dataset. |

Change detection is `stat`-only: a dataset's *revision* is the inode, size and nanosecond mtime of its `meta/` files; a raw capture's fingerprint covers every file and symlink. Catalog writes take a cross-process file lock (`outputs/LEVI/workbench/.catalog.lock`), so several LEVI processes can sync one workspace. Dataset files are served with `Cache-Control: no-cache` so browsers revalidate rather than reuse stale copies. **Unregister** in the Workbench removes an entry without touching files (auto-sync re-adds it if it is still in the workspace).

## Processes LEVI starts, and how they stop

Set `LEVI_CPU_ONLY=1` before starting LEVI for a strict CPU-only session: the idle GPU watcher is not started, GPU status never invokes `nvidia-smi`, and local accelerator-backed inference and SAM3 worker launch are blocked. An external Codex/Claude or remote API provider can still be used. A project-scoped MCP connection created with this flag carries it into the client; a CPU-only call refuses to reuse a core started without it, so stop that core first.

One core process serves the web UI, the REST API and every MCP bridge. It is started by `levi serve` or by the first MCP call, and it keeps running after the browser or the agent goes away. `uv run levi stop` stops it; `uv run levi stop --all` also stops the Ollama service LEVI started itself (never one you run). While idle the core samples the GPU (every 15 s) and the workspace, which costs about 1 % of one CPU core.

Everything else the core starts runs in its own process group and is recorded, with the core's identity, in `outputs/LEVI/workbench/processes.json`:

| Process | Ends when | Also stopped |
| --- | --- | --- |
| SAM3 worker (object annotation, from the page or an agent) | its job finishes or is cancelled | past `LEVI_SAM3_TIMEOUT_SECONDS` (default 21600) or after `LEVI_SAM3_STALL_SECONDS` (default 1800) without progress (page jobs); past the run's time budget (agent jobs) |
| Conversion job | it finishes or is cancelled | after 24 h |
| DROID test-sample download | the draw is ready, skipped for space, or cancelled (`levi sample cancel`) | — (the next core start resumes an interrupted draw) |
| Codex / Claude Pilot runtime | the session is paused, cancelled or runs out of turns or time | when its run finishes, or after `LEVI_PILOT_IDLE_SECONDS` (default 600) with no message; resume starts a new session |

When the core stops normally it terminates every group it started. When it was killed instead (SIGKILL, out of memory, a crash), the next start of the core, or `levi stop`, terminates the groups whose owner is gone. A process is signalled only after its start time, boot and executable match the record, so a reused PID is never hit.

The local model is not a child process: Ollama keeps it in GPU memory for `LEVI_OLLAMA_KEEP_ALIVE` (default `2m`) after LEVI's last request, so the GPU frees soon after a run finishes, fails or waits for a person. Pausing or cancelling a run cuts its model request in flight, and Ollama stops generating. The GPU guardian ([Local models](OLLAMA.md#4-shared-gpus-the-guardian)) unloads the model when other work needs the GPU.

## Two different "converted dataset" concepts

### Product 1: input → export target (`levi/conversion/`)

Inspect → choose a target → run (see [Conversion](CONVERSION.md)). Targets: `lerobot_v21` and `recap_value` ([RECAP](RECAP.md)); inputs: raw captures (`robot_capture`, `image_sequence`) and LeRobot v2.x datasets (`lerobot`, for RECAP re-export). Output structure: `meta/info.json`, `meta/episodes.jsonl` (`source_demo`, `levi_outcome`), `meta/episodes_stats.jsonl`, `meta/tasks.jsonl`, `meta/stats.json`, `meta/levi_provenance.jsonl` (`source_positions`, `source_frame_ids` per episode), `meta/levi_conversion.json`, `data/chunk-000/episode_*.parquet`, `videos/chunk-000/<camera feature>/episode_*.mp4`; RECAP adds `meta/returns.parquet`, `meta/episode_labels.csv`, `meta/levi_recap.json`.

### Product 2: registered dataset → annotated training dataset (`backend/app.py:_do_export`)

- Input is any registered LeRobot dataset (not a raw capture's browsing view — convert that instead; annotations carry over).
- Path: `outputs/LEVI/exports/<name>_annotated/`.
- Structure: `meta/` (adds `language_persistent`/`language_events` features, the `say` tool schema, and human outcome labels as `levi_outcome` + `levi_outcome_source`), `data/*.parquet` with the annotation columns, `videos/` (hard-linked/copied), `annotations/language/` audit mirror, `annotations/sam3/` snapshot.

### Browsing views of raw captures

`outputs/LEVI/workbench/views/<name>/`: a LeRobot-shaped view of a registered raw capture (every frame, lossless retime, `meta/levi_view.json`) so it can be browsed and annotated before conversion. The catalog entry (`kind: raw`) keeps the capture as `path` and serves the view; annotations are keyed by the capture's name and carried into conversions made from it. A 171-demo capture's view builds in about 38 s, by remux only.

## Supported raw capture formats

Four raw input profiles are recognized, two of them currently have direct training conversion writers. DROID raw is browse/annotate only.

### Type 1 — teleoperation capture (e.g. `data_collection_robotiq`)

**Format**: a `demo_NNNN/` folder with `end_effector_pose.csv`, `gripper_state.csv`, `events.csv`, `metadata.json`, `wrist_camera.mp4`/`side_camera.mp4` (or `<camera>_raw.avi`); `task_description.txt` one level up, shared by every demo under that task. `success_flag` sits at its default `0` in practice (an operator-toggled "keeper" flag rarely pressed); no `data_source` field; a camera stall is the flat `metadata.json["camera_stalled"]` boolean; the terminal `events.csv` row is `stop_demo`.

**Pipeline**: Workbench (inspect → export) or `uv run levi convert pipeline --source <capture dir> --output <dir>` → single-pass conversion (FPS auto-lowered to the measured rate in `resample` mode, optional static filter, lossless `retime` mode) → preflight gate → validation → atomic publish.

**Output**: Product 1, above. No `levi_outcome` (meaningless for teleoperation data); RECAP export only as `sft` demonstrations or after labeling outcomes.

**LEVI features available**: full episode browsing, Statistics/Filtering/Frame gallery, Action Insights (full dataset / range / by task), Doctor diagnostics, language/event + SAM3 object annotation, review flagging + export, "export annotated dataset" for training.

### Type 2 — policy-rollout / online-RL eval capture (e.g. `online_rollout_data`)

**Format**: identical CSV/video layer to Type 1, plus `metadata.json["data_source"] == "policy_rollout"` and an `eval` block (`outcome`, `checkpoint_dir`, `steps`, …). `success_flag` is the real task outcome (0/1, matching `eval.outcome`); a camera stall is the nested list `metadata.json["cameras"]["stall_detection"]["stalled"]`; the terminal event is `episode_end`.

**Pipeline**: identical command/UI to Type 1 — `data_source` is detected automatically, no extra configuration.

**Output**: Product 1, with `meta/episodes.jsonl` additionally carrying `levi_outcome: "success" | "failure"` per episode.

**LEVI features available**: everything Type 1 has, plus a success/failure dot per episode in the sidebar and a "Failures · N" filter (single-task datasets, typical for eval captures, correctly hide the task filter rather than erroring).

Verified end-to-end against a real batch: the `pick_screws_of_same_size_into_the_empty_slot_of_screw_box` eval task, 171 demos, 124 failure / 47 success, converted with plain default options (FPS auto-adjusted from the requested 10 to the measured 9.395653) to `pick_screws_of_same_size_into_the_empty_slot_of_screw_box_lerobot/`. It inspects as supported for `lerobot_v21` and supported-with-warnings for `recap_value`.

### Type 3 — image-sequence capture

**Format**: `wrist_camera/`/`side_camera/` as directories of naturally-sorted PNG/JPEG instead of `.mp4`; `source_fps` must be set explicitly (nothing to probe).

**Status**: input format `image_sequence`, fixture-verified only; neither real corpus above uses it.

### Type 4 — DROID raw HDF5 and MP4

A dataset root contains `demo_NNNN/trajectory.h5`, one `metadata_*.json` and `recordings/MP4/` with three camera recordings. Install `uv sync --locked --extra droid` to inspect it. LEVI validates the HDF5 trajectory fields, indexes all demos and builds a read-only v2.1-shaped browsing view under `outputs/LEVI/workbench/views/<name>/`. The input folder remains untouched; annotation versions stay under the raw dataset name. Direct training conversion is unavailable until an explicit DROID action/time/schema mapping is supplied. The nominal view clock is 14.3 FPS, and `meta/levi_provenance.jsonl` retains each source control timestamp and the per-episode maximum source/view difference. Do not treat nominal time as exact capture time. See [Conversion](CONVERSION.md#droid-raw-browsing-view).

### DROID test sample

A workspace LEVI creates is offered a test dataset: the first time the core runs in it, it draws **500 episodes of the public DROID raw 1.0.1 release** (`gs://gresearch/robotics/droid_raw/1.0.1`, read anonymously) into `droid_raw_500_draw01/`, in the background (`levi/samples/`). It is a Type 4 capture, registered by the runtime sync once complete.

- **Selection** follows the reference subset script: every episode's `metadata_*.json` is listed, grouped by (lab, outcome), and drawn round-robin with seed 42 — no language or success filter. Draw 1 is that script's `selected_500`. `levi sample draw` (or `POST /api/levi/samples/droid_raw`, `size` 1–5000) makes another draw, `droid_raw_500_draw02/` and so on, from the same order: draws never share an episode, and the same draw number is the same episodes on every machine. An episode LEVI's DROID reader rejects is replaced by the next one of the order and named in `_meta/selection.json`.
- **Files kept**: `metadata_*.json`, `trajectory.h5` and the non-stereo `recordings/MP4/*.mp4` (about 35–60 MB per episode, 18–30 GB per draw); no SVO, no stereo MP4. Every file is checked against the bucket's size and MD5 before it counts.
- **Space** is not checked: a draw of 500 episodes is 18–30 GB. `levi sample status` shows the disk's free space and each draw's planned bytes.
- **Resuming**: a draw stopped by the core stopping, a lost network or `levi sample cancel` keeps its partial folder; the next start (`levi sample draw`, the API, or the core starting again for an interrupted draw) fetches only what is missing. An unfinished draw is always resumed before a new one is planned. `levi sample cancel --discard` (API: `?discard=true`) deletes the partial folder instead and closes the draw; the next draw then takes the same positions of the order. The automatic first draw happens once, and only if no draw was made by hand first.
- **Settings**: `LEVI_DROID_SAMPLE=off` means LEVI never starts or resumes a download by itself (tests and CI set it; drawing by hand still works); `LEVI_DROID_SAMPLE_WORKERS` (1–8, default 4) sets parallel episode downloads.
- `levi sample status` shows the ledger, progress and free disk space; `levi sample draw [--size N] [--workers W]` draws in the foreground with progress (Ctrl-C stops, drawing again resumes); `levi sample cancel [--discard]` stops a background draw. `GET /api/levi/samples` and `POST /api/levi/samples/droid_raw/cancel` are the same for the web UI.

### Already-converted LeRobot datasets

Not a "raw" format, but relevant: LEVI can register and fully use **any** v2.0/v2.1/v3.0/v3.1 dataset directly — regardless of what produced it (LEVI's own converter, an external script, or a Hub download) — with no conversion step. A dataset produced by an external conversion script is registered as-is and labelled that way in the dataset list.

## Shared Agent Core

`outputs/LEVI/workbench/agent/core/` holds the private socket, instance and human control key. `agent/connections/<client>-<timestamp>/` holds scoped machine-local credentials and a record of the project MCP entry the connection wrote. Pilot sessions and artifacts use the existing per-dataset run directories; see [Pilot](PILOT.md). Never publish these runtime files.


## Local model state

`outputs/LEVI/workbench/agent/workbench.sqlite3` also stores model manifests,
download jobs and annotation-phase teaching records. These are durable records,
not cleanup caches. A LEVI-owned Ollama instance uses
`outputs/LEVI/workbench/models/ollama/` for its process identity, home and log
(plus the GPU guard's `gpu-last-busy.json`) and `checkpoints/ollama/` for model
storage. Ollama's internal content-addressed blobs are the one naming exception;
do not rename them. An Ollama service you run yourself keeps its own paths. See
[Local models](OLLAMA.md).
