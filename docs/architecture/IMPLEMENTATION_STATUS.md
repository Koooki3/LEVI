# Unified execution architecture: implementation status

This records actual incremental changes toward the approved 0.4.0 plan. Package
version remains 0.3.0. No tag, release, commit or remote repository mutation is
part of this update. Existing execution and format services remain authoritative
until their replacements pass migration and equivalence gates.

## Implemented and connected

- `levi/domain/contracts.py`, `levi/tools/registry.py`: strict versioned tool and
  recipe contracts, schema matching, deterministic DAG ordering and downstream
  invalidation. Discovery never invokes a handler. Returned metadata cannot
  mutate registered schemas. This is a foundation, not the replacement executor.
- `levi/domain/schema_catalog.py`: deterministic contract snapshot;
  `uv run levi dev check-contracts` checks drift. CI calls the same command.
- `levi/inference/`: native pinned loopback Ollama transport, digest-bound model
  requests, explicit inventory/binding/download/memory controls and persisted
  cancellable download jobs in the existing SQLite store.
- `levi/inference/runtime.py`: optional dedicated installed Ollama process,
  workspace-owned directories, minimal child environment and PID identity checks.
  An installer and OS-enforced offline network sandbox are not implemented.
- Existing `ProviderConfig`, credential status, provider routing, task approval,
  evidence checks and draft publication now support Ollama without a fictitious
  API key. Model changes invalidate old task bindings. No implicit download or
  cloud fallback occurs.
- `levi/agent/supervision.py`: assigned external teacher feedback gates existing
  annotation phases. Feedback is scope/revision checked, stored transactionally,
  idempotent and separate from plan/pilot/commit authorization. Cached learner
  phases resume without another model call. Revoked teachers block progress.
- REST and MCP use the existing Registry for `supervision.pending/feedback`.
  An external agent cannot bypass the teacher gate using another mutation tool.
- Bilingual model/connection cards include actual model-layer progress, explicit
  cancellation, memory controls, owned service paths and teacher status via SSE.
- `levi/harness/` (2026-09-22, verified by the offline and online end-to-end
  tests on `stack_the_plates_of_same_color_together`): bounded task closure
  (ledger, verified dataset memory, deterministic triage), the §6.6
  improvement state machine with LEVI-computed evaluation, one revision,
  human-only publication and observation, and plan-time freezing of harness
  parameters. MCP/REST: `quality.inspect`, `memory.get/search/rebuild`,
  `improvements.list/get/evaluate/revise/transition`, `evidence.changes`.
  CLI: `levi agent improvements …`, `levi agent memory …`. Paths follow §10.2
  (`datasets/<name>/{tasks,reports}`, `workbench/{memory,improvements}`).
  Cost (`levi/harness/cost.py`): every closed run gets one token/time record
  whatever the agent -- `metered` for API/Ollama runs, `reported` or LEVI's
  measured `delivered` lower bound for external MCP agents -- folded into a
  per-dataset, per-agent `cost_profiles`; triage files cost rises, slow tools,
  oversized responses and a publishable `evidence.mosaic_tile_width`; the
  in-process runtime applies published refinement windows too; a startup
  sweep closes runs that ended unnoticed. MCP `cost.profile`; `plans.estimate`
  adds a `local` figure. Open items are tracked locally (`.state/memory/issue-register.md`).
  Local VLM learner (2026-09-22): Ollama 0.34.2 + `qwen3.5:4b` (Q4_K_M, vision),
  LEVI-owned service on 11435, profile `qwen-local`. `harness/context.py` puts the
  dataset memory, teacher notes and held-out examples into every local-model
  prompt; `harness/teaching.py` turns each supervision feedback into
  `datasets/<name>/teaching/<run>/episode_NNNNNN-<phase>.json` and a memory note;
  `harness/tasking.py` interprets a natural-language request into a checked
  task spec (CLI `levi agent task`, web task console) and drives its steps
  through the human gates; `harness/grading.py` grades a learner against a
  teacher reference (`teaching/reference-<task>.json`, `teaching/grades.json`).
  `inference/gpu.py`: off-peak guard -- the local model never runs while
  another process computes on the GPU or left it < 300 s ago; the service
  unloads resident models the moment one appears.
  Not yet: FTS5 index, semantic retrieval, reflection-model triage,
  `validation/`.
- English/Chinese local-model guides, API reference, workspace paths and notices
  describe installed behavior and its limits.

## Verification scope

Tests include deterministic contracts, fake native model HTTP responses,
shared-Harness pilot and teacher feedback, raw-source hash preservation,
credential isolation, download cancellation/idempotency, mocked process identity,
and opt-in production-browser interaction with fixture APIs. No Ollama process,
GPU/CUDA probe, checkpoint download or real model inference is run by these tests.
See [validation record](../VALIDATION.md) for recorded execution results.

## Work-package state

| Work package | Actual state |
| --- | --- |
| WP00 baseline | Existing tests/regression checks retained; full migration backup rehearsal pending |
| WP01 contracts | Partial: Python contracts and drift snapshot; complete single-source TS/MCP/CLI generation pending |
| WP02–WP07 data/runtime/formats/review | Existing services preserved; planned unified replacement not implemented |
| WP08 model loop/UI base | Partial: native provider routed through existing annotation Harness; generic tool loop pending |
| WP09 Ollama/offline | Partial: local service integration, explicit downloads and owned-process lifecycle; installer, capability inference probes, offline bundle and shared resource arbitration pending |
| WP10 Harness/memory | Partial: task ledger, per-dataset verified memory with plan-time context, rebuild; FTS5/semantic index and dependency-cache migration pending |
| WP11 supervision/improvements | Partial: annotation-phase teacher gate; improvement candidates with deterministic triage, LEVI-computed evaluation, human publication, observation and retain/rollback (harness parameters only); competency promotion, general tool supervision and skill-text learning pending |
| WP12 six-area UI/governance | Existing UI extended; systematic navigation/state/governance refactor pending |
| WP13 migration | No existing database tables removed; full migration/rollback and isolated-offline acceptance pending |
| WP14 delivery | Incremental docs/CI updated; full 0.4.0 SBOM and distribution gate pending |

Shadow and supervised modes currently use the same explicit teacher gate and are
recorded separately for later evaluation. They do not establish autonomous
competence or implement weight training. Human resumption is required after
feedback. Real-client teaching quality, GPU throughput and training benefit are
unverified. Do not mark the full architecture plan complete from this increment.

## Sources

Original adapter code follows the [Ollama chat API](https://docs.ollama.com/api/chat)
and [pull API](https://docs.ollama.com/api/pull). No upstream model weights or
Ollama binaries are redistributed. See [third-party notices](../../THIRD_PARTY_NOTICES.md).
