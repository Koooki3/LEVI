# Changelog

## Unreleased — Agent Pilot

- Shared local Core Host for headless MCP and the existing Web UI.
- Scoped, revocable Codex/Claude connections and human terminal approvals.
- Optional pinned ACP adapters, public event streaming, task-state recovery and artifact manifests.
- Real-client/model quality acceptance remains separate from fixture tests.

## Unreleased

- Experimental Agent Harness: executable plan approval, pilot acceptance, budget revisions, workflow-specific skills, content-keyed evidence/model reuse, temporal definitions and boundary refinement, persisted draft masks in the existing player, and native export roundtrip validation. See `docs/HARNESS.md` for limits and manual quality gates.


- Experimental Agent Workbench: bounded evidence, optional compatible model adapter, scoped stdio MCP, durable tasks and human-reviewed ChangeSets.
- Optional SAM3 object assistance with staged mask review; original data and existing object annotations stay preserved.
- Focused review queue, batch decisions, account profile cards and conflict-checked undo drafts.
- Dataset/annotation adapter registry and readable dataset-scoped run/version/cache paths.
- Real-model quality, GPU inference, HTTP MCP and ACP runtime acceptance are not claimed by this source update.

## 0.3.0 — Dataset indexing and review hardening

- Canonicalized LeRobot dataset-version detection for supported v2.0/v2.1/v3.0/v3.1 metadata and rejected malformed or unsupported versions consistently across the frontend and backend.
- Rebuilt dataset task indexing around authoritative task indices, JSONL/Parquet fallbacks, episode metadata, and frame-level mappings so task counts and sidebar/insights filtering use the actual dataset contents.
- Hardened episode metadata, data-path resolution, timestamp snapping, numeric chart values, and diagnostics against malformed, fractional, non-finite, duplicate, or stale records.
- Added bounded LRU-style caches, in-flight Parquet request sharing, and authentication-change invalidation to prevent private-data leakage and unbounded browser memory growth.
- Fixed task-filter navigation, empty-result handling, statistics counts, v3 episode-length support, language-instruction extraction, and review-tab state restoration.
- Updated release metadata, validation records, and source notices for the v0.3.0 source release.

## Unreleased

- Modular conversion: a format registry (inputs `robot_capture` with teleop/policy-rollout variants, `image_sequence`, `lerobot`; outputs `lerobot_v21`, `recap_value`), an input inspection with a per-requirement checklist and per-target compatibility (reasons and one-click fixes), and contract tests over every pair.
- Single-pass parallel conversion (2 decodes + 1 encode per camera instead of 9 + 3) with a lossless, exact `retime` mode, staged atomic publishing and structured live progress in the Workbench.
- RECAP value dataset export (π\*0.6): per-step rewards, terminal `is_success`, RLinf-compatible returns sidecar, LeRobot-proposal episode labels and a manifest; from raw captures or existing LeRobot datasets.
- Raw captures can be registered, browsed and annotated through a lossless view; annotations, outcome labels and SAM3 masks carry over into conversions by source demo. Features that need a converted dataset show an explanatory note instead.
- Human success/failure episode labels (click the sidebar dot), used by the Failures filter, RECAP export and annotated exports.
- The dataset list shows each entry's format, version and origin.
- Runtime workspace sync: new datasets and raw captures are registered, in-place changes refresh the catalog and the annotation backend, changed raw captures get their view rebuilt with annotations re-keyed by demo, removed datasets drop out (annotations kept), open viewers offer a reload; cross-process catalog lock; `Sync now` and `Unregister` in the Workbench.
- Hash-free naming across the workspace (catalog names per dataset, timestamps per run) and `levi migrate` for older workspaces; legacy `/local/<hash>` links redirect.

- English is now the default UI and repository landing language; the 602-key English/Chinese catalogs stay in parity, and the welcome page, CLI and documentation expose a professional bilingual path.
- Hardened standalone-browser behavior: parent-frame messaging is best-effort, browser storage failures fall back to in-memory state, and Ctrl/Cmd+S, undo/redo and playback shortcuts remain inside the workbench.
- Annotation label popups now open at the viewport center, stay within the viewport while dragging, and expose localized dialog names and drag affordances.
- Added global SAM3 object annotation for demos, Hub and registered local datasets. The 1038lab/sam3 checkpoint now has an authenticated browser/CLI download action with resumable workspace progress, retry handling and an explicit ready gate before real jobs; browser/CLI account scopes prevent stale private-data reuse, and native LeRobot files remain read-only.
- Action Insights now scopes to the full dataset, an episode-index range or a single task, with a sample cap that can be set to "All" for full coverage; the panel reports how many of the in-scope episodes were analysed.
- Added a task filter to the episode sidebar for multi-task datasets, backed by a shared dataset-wide task ↔ episode index.
- Replaced the fixed 120-episode ceiling with a configurable budget, concurrency-limited parquet reads and load progress.
- Autocorrelation now sizes its lag horizon from the 25th-percentile episode length instead of the shortest episode, so one truncated episode no longer blanks the chart on a full-dataset pass.

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
