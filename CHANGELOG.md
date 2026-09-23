# Changelog

All notable changes to LEVI. Versions follow the tags on GitHub; everything under **Unreleased** is on `main` but not yet in a tagged release.

## Unreleased

### Local models and natural-language tasks

- Native Ollama provider with digest binding, bounded transport, explicit downloads with persisted progress and cancellation, and load/unload controls; an optional LEVI-owned Ollama service with workspace model storage, `OLLAMA_NO_CLOUD`, explicit start/stop and Linux process-identity checks. Default profile: `qwen3.5:4b`. Schema-bound calls run with reasoning off.
- **GPU guardian.** Local-model requests yield to other GPU work by policy (`protect` / `share` / `ignore`, `workbench/models/gpu-policy.json`), wait a quiet window learned from how each workload restarts, unload resident models the moment protected work appears (a cut-off request is a preemption), and resume GPU-blocked runs automatically. `levi agent gpu` / `gpu.status` explain the decision.
- **A small model is fenced in by its schema.** Per-request output schemas allow only the plan's kinds and subtasks and the evidence ids actually shown; intervals must carry an end, an outcome and an evidence note; refinements keep the draft's intervals one for one, and boundaries moved beyond the frames shown keep the draft's. Invalid answers keep their valid proposals (for the teacher, or dropped and recorded without one), and their tokens are always settled.
- **Requests sized to the model.** Compact evidence rows; per-image and per-character token cost learned from the model's metered requests (calibrated once); refinement images fitted to the context window; long episodes refined in batches; the answer budget sized to the intervals expected; Ollama context overflows reported with their numbers.
- **Natural-language tasks.** `levi agent task new "…"` and a task console in the Workbench (which remembers the viewer's last task across reloads): the local model turns a sentence into a task spec, LEVI checks it against the catalog, a person approves it, and `advance` runs each step up to the next human gate, ending with a token and time report.
- **Teacher supervision and teaching memory.** A scoped external agent can accept, revise or reject each learner phase. Feedback is kept per phase under `datasets/<name>/teaching/` and becomes a note in the dataset's memory. Every local-model prompt carries a bounded brief of that memory, frozen at plan time and never containing the episodes being annotated. Teacher references and grading (`teaching/reference-<task>.json`, `teaching/grades.json`).

### Harness: task closure, cost, memory and self-improvement

- Every finished run is closed exactly once (a service restart closes any that ended unnoticed): a task ledger (`datasets/<name>/tasks/<run>/ledger.json`), a dataset memory of human-committed facts (`workbench/memory/<name>.json`) and deterministic triage.
- **Cost as part of the harness.** One token and time record per run for API, local and external agents (`metered`, `reported`, or LEVI's measured lower bound), with a breakdown and waste signals; per-dataset, per-agent cost profiles feed `plans.estimate` (`local`), `cost.profile` and the next plan. The web UI's own calls are never counted as agent tokens.
- **Self-improvement loop.** Improvement candidates move `proposed → evaluating → qualified → awaiting_authorization → published → observing → retained / rolled_back`; LEVI computes the evaluation on stored evidence, one revision is allowed, a person publishes. Publishable parameters: `evidence.refine_top_k`, `evidence.mosaic_tile_width`. Cost-rise, slow-tool and oversized-response findings are filed automatically.
- `quality.inspect` exposes the structural quality checks to agents; reports are kept per dataset per hour (`reports/quality-<YYYYmmddTHH>.json`), also from the Doctor.
- `evidence.changes` ranks coarse intervals by visual change; with a published `refine_top_k`, `runs.prepare` returns `refine_first` and the in-process runtime refines those windows too. `evidence.read` accepts `images: false`.
- **Built-in knowledge.** Dataset-agnostic rules ship in `levi/knowledge/` (annotation, task interpretation, harness). Every model LEVI runs receives the rules for its job, and external agents get the annotation rules from `workspace.get_context`. Every run closure refreshes the list of local notes that could join them. A person promotes a candidate (`levi agent knowledge promote`, rewriting it to be general) or rejects it. The check refuses text that names episodes or times.
- **Evaluation records.** An agent's committed subtask annotation of a whole dataset writes `eval/<dataset>_agent_<YYYYmmddTHH>_<driver>.md`, where the driver is `external-mcp`, `external-pilot`, `api`, `local-vlm` or `local-vlm-teacher`. The record covers completion, second- and frame-level coverage, semantic richness, structural correctness and agreement with same-content dataset copies, the same measures as a person's recording. It adds efficiency and non-local (external) token accounting.
- **Docs keep up with the code.** `levi docs check` (also run by `levi check` and the tests) lists every capability, `levi agent` command and user-facing setting missing from the docs, every broken relative link and every stale generated section. `levi docs sync` regenerates the capability reference and the knowledge index.
- `reset` now also removes a dataset's teaching phases, cached model answers, tasks, task ledgers, teaching files, memory, improvement candidates and publication receipts.
- CLI: `levi agent task`, `levi agent improvements`, `levi agent memory`, `levi agent knowledge`; `levi docs`; `levi agent disconnect` removes the MCP entry it wrote.

### Agent workbench

- **Recorded signals in the evidence.** The first `evidence.read` page of an episode carries `signals` from its state/action columns (gripper close/open, height turns, still spans, to the frame); `evidence.refine` takes `window_seconds` for a narrower look. Answers dropped fields a reader of times cannot use (`ledger_digest` and `next_offset` on a whole-episode mosaic, the refine policy block, float noise), and `propose_segments` returns counts instead of every remaining episode. Signals are given without interpretation (neither boundaries nor attempts). `evidence.read` takes `episodes: [...]` (up to four whole episodes in one call). Agents stage what they have decided before reading more, in the same turn as the next read, and refine only what a sheet cannot settle.
- **Dense looks at a chosen step, several episodes per call.** `evidence.refine` takes `ranges` (spans watched at `step_seconds`) besides `around_seconds`, and `episodes: {N: {...}}` for up to four episodes per call; dataset adapters gain `sample_frames`. Built-in rule annotation-012: `unknown` is for what the recording does not show, not for what was not looked at.
- **Boundary checks and re-staging.** `evidence.boundaries` shows, per staged episode, one row of frames around every boundary with the plan's start/end definitions; `propose_segments` `replace: true` re-stages an agent's own episode in place (never one a person reviewed). `evidence.refine` is now covered by the plan's media-egress authorization like every other image-returning read.
- **Namespaces: one input, many isolated experiments.** `<dataset>--<namespace>` (`levi namespace create|list|remove`, `GET/POST /api/levi/catalog/{name}/namespaces`) is a catalog entry of its own over the same source folder and view: annotations, runs and the active head, memory, teaching, examples, eval records and cost profiles are separate per namespace, nothing is copied, and namespace notes never become knowledge candidates. Path lookups answer the base dataset; the annotation backend keys its state and sidecars by catalog name.
- **DROID test sample.** A workspace LEVI creates draws 500 episodes of the public DROID raw release in the background the first time the core runs (`levi/samples/`): the reference subset script's seeded round-robin across (lab, outcome), metadata + trajectory + non-stereo MP4 only, every file checked by size and MD5, resumable and assembled in a hidden `.partial` folder (the first draw: 11.6 GiB, about 24 MiB per episode; free space is not checked). `levi sample draw` / `POST /api/levi/samples/droid_raw` draws another (`droid_raw_<size>_drawNN`, the workspace's next unused positions of the order, recorded in `_meta/selection.json` — the positions, not the name, identify a draw); `LEVI_DROID_SAMPLE=off` (or `0`/`false`/`no`; an unknown value too) disables the automatic draw. Ctrl-C and `levi sample cancel` stop a draw promptly; cancel refuses a draw in a process it cannot identify, and `--discard` deletes only once the draw stopped. A draw published just before its process stopped, or a sample folder whose ledger was lost, is recorded rather than drawn again. The core resumes a draw left interrupted or failed on the network, at most 3 times. Each listing page and file is one retried request; a refusal or a full disk is not retried.
- **Problems only.** A staging receipt names `problems` for an episode only when its segments break a general rule (`levi/agent/checks.py`: two intervals of one subtask meeting, an `unknown` the next segment settles, never for `other` (annotation-004) -- right 78-90% of the time against a full-dataset reference; a signal-based "two engagements" check, right about half the time, was dropped); guidance no longer asks for a routine boundary check of every episode (measured: 20 checks, one re-staging).
- **Attempts, stated once.** Built-in rule annotation-007 now defines an attempt as one continuous engagement with an object (each its own interval and outcome, a new approach between two, failure when the effector leaves without the object) after manipulation-annotation practice (Rubicon Boundaries, MimicGen, REBOOT, AgiBot World); it replaces the earlier rule that merged repeated attempts. `temporal-annotation` skill @6, `levi-overview` @12.
- **Task-agnostic grading.** `harness/grading` drops the stacking-specific `place_agreement` for `time_accuracy` (share of the reference's annotated time with the same subtask) and `outcome_time_accuracy` (same subtask and outcome); evaluation records show both.

- One capability dispatcher for UI, REST, stdio MCP and CLI; executable plan approval, human pilot gate, budget revision, workflow-specific skills and content-keyed evidence and model reuse.
- The plan form offers what the dataset declares: episodes, cameras and tasks are clicked or dragged; spending limits appear only for a configured model endpoint.
- Evidence follows the approved observation policy, can be read as one labelled contact sheet per page (`layout: "mosaic"`) and refined around unresolved boundaries (`evidence.refine`).
- `runs.list` tells an agent whose turn each run is; `workspace.get_context` orients a first-time agent; **Live activity** streams every call, refusal and artifact path.
- Object masks: `objects.strategy` chooses SAM3 or the agent; `objects.detect` measures candidate regions without a model; `objects.propose` accepts `candidate_id` and links identities by overlap. Masks carry between annotated frames in the viewer, dashed and labelled.
- `changes.rebase`, `runs.abandon`, `levi agent clean` (regenerable files only) and `levi agent reset` (one dataset's agent history).
- Managed Codex/Claude Pilot with scoped, revocable connections and a shared local core.
- Readable identifiers: `<kind>-YYYYmmddTHHMM`, `-2`… only on a clash; never a hash.

### Conversion, raw captures and RECAP

- Modular conversion: input formats `robot_capture` (teleoperation / policy rollout), `image_sequence`, `lerobot`; outputs `lerobot_v21` and `recap_value`; input inspection with a requirement checklist and per-target compatibility.
- Single-pass parallel conversion (2 decodes + 1 encode per camera instead of 9 + 3), lossless `retime` mode, staged atomic publishing and live progress.
- RECAP value dataset (π\*0.6): per-step rewards, terminal `is_success`, RLinf-compatible returns, episode labels and a manifest.
- Raw captures register as datasets through a lossless browsing view; annotations, outcome labels and SAM3 masks carry over into conversions by source demo. Human success/failure labels per episode.

### Human annotation

- **Recording** in the annotation view: Start, then Stop, writes `eval/<dataset>_human_<YYYYmmddTHH>.md`. The record gives the duration, episodes confirmed while the session ran, subtask count, second- and frame-level coverage, semantic richness, structural correctness, agreement with same-content copies and efficiency, line by line comparable with an agent's record. The session is kept on the server and survives a reload.
- **Confirm episode complete**: an explicit per-episode state (`annotations/<name>/status/`), separate from saving.
- **Subtask vocabulary** per dataset: human spans carry a subtask id and an outcome picked from a list, like agent segments, and the description is optional. The last agent plan's subtasks are offered as a start. Without a vocabulary, subtasks stay free text.
- The natural-language task console remembers the viewer's last task across reloads.

### Workspace, sync and interface

- Runtime workspace sync (discover, refresh, rebuild views, drop removed datasets) with a cross-process catalog lock.
- Hash-free naming across the workspace and `levi migrate` for older workspaces; legacy `/local/<hash>` links redirect. Exports and reviews download as `<name>-quality|review-<YYYYmmddTHH>.json`.
- English is the default UI and repository language, with a complete Chinese catalog. Safe browser-storage fallbacks, in-workbench save/undo shortcuts, centered and draggable annotation popups.
- Global SAM3 object annotation for demos, Hub and local datasets with an authenticated checkpoint download; Action Insights scoped to the dataset, a range or a task; task filter in the episode sidebar; configurable episode budget.

### Fixes

- **An external agent annotates a dataset in fewer turns.** Measured against the same task without LEVI, an agent spent its extra tokens on paging and bookkeeping, not on looking: a mosaic page now holds up to 32 frames by default (one call per episode instead of paging by 8); `evidence.refine` answers with a sheet of just the added frames and, past the frame cap, refines what fits and names the rest instead of refusing; `annotations.propose_segments` lets an external agent leave evidence ids to LEVI (it cites the frames it showed inside each interval), uses a success's content as its evidence note, and snaps a boundary within half a frame of the first or last frame. Contact sheets are JPEG and a refinement's frames share one sheet (split past 30 tiles), sampled at the plan's boundary tolerance: PNG sheets of 1–2.4 MB each overflowed an agent's request and silently removed the images it had read. `levi agent call <capability> @-` gives shell-based agents what the MCP bridge gives MCP clients, images included. A capable external agent also works with less ceremony: its plan samples one frame per second on six-column sheets of up to 48 frames (one read per episode), the first read of an episode prepares it (`runs.prepare` optional), `annotations.propose_segments` accepts `segments: {episode: [{start, end, subtask, outcome, description}]}`, mosaic answers list frame times rather than evidence ids (`ids: true` adds them), and the person approving a plan may waive the separate pilot (`require_human_pilot: false`). `changes.edit` takes `episodes` to replace only those episodes, refuses an edit that would silently drop staged episodes, and answers an agent with a receipt. Refused agent calls are now a cost signal (ledger, hints and the evaluation record).
- `other`/`unknown`/`background` are never counted as outside a vocabulary; `supervision.pending` lists only waiting phases unless asked for history; an evaluation record can be written for annotation made outside LEVI (`record.imported`).
- **Nothing LEVI starts outlives it.** SAM3 workers, conversion jobs and Pilot runtimes are recorded in `workbench/processes.json`: a normal service stop terminates their process groups (a SAM3 worker used to keep running on the GPU), and the next start or `levi stop` reclaims the groups a killed service left behind. A process is signalled only after its identity is checked. `levi stop` now waits for the service to exit, and `levi stop --all` also stops LEVI's own Ollama.
- A SAM3 job from the page is stopped past `LEVI_SAM3_TIMEOUT_SECONDS` or after `LEVI_SAM3_STALL_SECONDS` without progress; it had no limit at all.
- Pausing or cancelling a local-model run cuts its request in flight, and Ollama stops generating; the model stays loaded for `LEVI_OLLAMA_KEEP_ALIVE` (default `2m`) after the last request instead of Ollama's 5 minutes.
- **A run's token total can be unlimited** (`budget.max_tokens: null`, or an empty Tokens field in the plan form); natural-language tasks plan without a cap. Each model request still reserves and settles its own context window, and calls and time stay bounded. A full-dataset local-model run no longer hits the old 1,000,000-token ceiling.
- A Pilot session ends, with its Codex/Claude process, when its run finishes or after `LEVI_PILOT_IDLE_SECONDS` without a message, instead of waiting out its whole duration budget.
- Re-annotating an episode no longer stacks a second run's segments on the first; agent-published segments are replaced, human-edited ones kept.
- Published segments keep their subtask, outcome, attempt and uncertainty (previously only text and times survived commit).
- Agent tokens were overstated when web-UI calls were counted as agent calls; propose, refine and mosaic answers no longer echo whole change sets or ledger rows.
- Call ids were shared by every call in the same minute; `reset` could delete records before files and leave orphans; a job thread could outlive its test and write into the real workspace.
- The GPU guard treated Ollama's own `llama-server` as a foreign process; the local-model transport timed out while a model was still loading.
- Signing in to Hugging Face no longer breaks local datasets; object masks draw again; errors name the evidence or subtask they reject.
- Tests wrote learned request costs into the live workspace (`models/request-cost/ollama.json`); GPU history, request costs and evaluation records are now isolated in every test.
- Finishing a natural-language task whose quality step had already run failed with `UnboundLocalError`.
- Chinese uncertainty notes were filed under `other`; they are now classified like English ones.

## 0.3.0 — Dataset indexing and review hardening

- Canonicalized LeRobot dataset-version detection for supported v2.0/v2.1/v3.0/v3.1 metadata and rejected malformed or unsupported versions consistently across the frontend and backend.
- Rebuilt dataset task indexing around authoritative task indices, JSONL/Parquet fallbacks, episode metadata, and frame-level mappings so task counts and sidebar/insights filtering use the actual dataset contents.
- Hardened episode metadata, data-path resolution, timestamp snapping, numeric chart values, and diagnostics against malformed, fractional, non-finite, duplicate, or stale records.
- Added bounded LRU-style caches, in-flight Parquet request sharing, and authentication-change invalidation to prevent private-data leakage and unbounded browser memory growth.
- Fixed task-filter navigation, empty-result handling, statistics counts, v3 episode-length support, language-instruction extraction, and review-tab state restoration.
- Updated release metadata, validation records, and source notices for the v0.3.0 source release.

## 0.2.0 — First public release

- Bundled the complete capture pipeline with strict alignment, source-safe snapshots, configurable maps/thresholds, H.264 encoding and measured statistics.
- Added CLI stages, source-change checks, structured reports and interrupted-job recovery.
- Made workspace configuration portable and removed external project dependencies.
- Consolidated docs and added explicit cache cleanup.

## 0.1.0 — Initial development

- Created LEVI from the Apache-2.0 LeRobot Dataset Visualizer baseline, retaining upstream functionality and attribution.
- Added original visual design, Chinese-default/English interface and the requested strawberry demonstration cards.
- Added independent uv environment, local Bun bootstrap, dual-service launcher and production packaging.
- Added local dataset registration, Range serving, review manifests, version-aware diagnostics and conversion job integration.
- Extended annotations to v2 metadata; added sidecar persistence, non-overwriting exports and stale-response protection.
- Added backend tests, real-browser validation, conversion integration check, documentation and publishing CI.
