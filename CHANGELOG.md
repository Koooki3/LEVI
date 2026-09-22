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
