# Agent Harness: audit, executable plan and verification

Status: the contracts below are implemented and tested. Real-model annotation quality and SAM3 GPU inference are measured separately and are not claimed here.

## Code audit (2026-09-20)

The audit includes the local Agent Workbench changes, not just the v0.3.0 Git baseline. Existing user changes are retained.

| Area | Reused implementation | Gap found / treatment |
|---|---|---|
| Data/version access | `backend/app.py`, `agent/media.py`, conversion readers | Native v2/v3 offsets and raw-capture views reused. Evidence now records requested dataset time, video PTS, decoded frame and mapping error; non-monotonic ledgers fail closed. |
| Player/timeline | `episode-viewer.tsx`, `video-overlay-canvas.tsx`, annotation timeline/context | No new player. Dataset adapters opt into temporal windows through `sample_temporal`; unsupported adapters fail explicitly. Evidence seeks the existing clock. Staged object windows load from persisted sidecars into the same overlay. Fine-grained temporal editing remains in the review queue and existing native timeline after commit. |
| Annotation state | `annotations/sidecar.py`, language atoms, outcomes, Agent ChangeSet | Preserve existing schema. Temporal identity/attempt/outcome/uncertainty retained in Agent provenance, separate from native language columns. |
| Tasks/recovery | SQLite journal, leases, bounded worker, independent SAM3 worker | Previously planning did not authorize execution. Added exact plan approval and human pilot gate at service/runtime boundaries. |
| Review/export | Shared bundle resolver, native `_do_export`, conversion carry-over | Reuse atomic publication. Added native export action and provenance/object roundtrip checks. |
| Configuration | Provider profiles, HF account, separate external scopes | Declared capabilities remain explicit. Tool-call declaration is no longer silently enabled by UI. Real endpoint capability probing still needs a separately authorized model call. |
| Skills/context | Six versioned skills, typed provider | Previously all skills entered every request. Load only overview, validation and selected workflow. Persist full observation ledger and use scoped paged recall. |

## Research and adaptation

Primary sources checked on 2026-09-20:

- [SoL-Pi](https://github.com/NVlabs/SoL-Pi), MIT: Action Fusion, ObservationPack, evidence-preserving reduction and opt-in native Pi compaction. Local source inspected at `74f6f97b4577e8160dcfa7f26e44d32863b99c2c`; its Pi compatibility is 0.84.2, while the current upstream README lists 0.85.1. The local checkout is not represented as upstream HEAD. LEVI adopts deterministic edit→validate and exact evidence recall concepts, **not** arbitrary shell fusion, a remote log-reducer model or Pi-specific compaction hooks. No SoL-Pi code is copied or installed.
- [Pi coding-agent](https://github.com/earendil-works/pi/tree/main/packages/coding-agent): session persistence and public extensions belong to that runtime. LEVI's model adapter cannot assume these hooks exist. External MCP agents retain their own conversation management.
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence): checkpoints and interruption are useful orchestration patterns. LEVI keeps its own existing SQLite journal and annotation transaction; no second orchestration framework is added. Conversation recovery is not annotation atomicity.

Code modules, skills and the runtime permission service remain distinct. Dataset text is evidence, never an authority to change budget or permissions. No shell tool or extra agent delegation is introduced.

## User sequence

Install/start using [AGENT_WORKBENCH.md](AGENT_WORKBENCH.md), then:

1. Select dataset/episode in the existing viewer. Open Agent Workbench; select a configured model or external MCP runtime. Choose **Dataset review**, **Video subtasks and events**, or **Visible object masks**.
2. Reuse known dataset/episode/provider values. Fill missing cameras, goal and media consent. For temporal work, edit definitions with observable start, end, success and confusion conditions. The example is editable and is not an inferred fact about your task. For masks, name target concepts; only visible-region masks are supported.
3. Inspect/create the plan. Clarification uses no model calls. Review explicit scope, model, budget, workflow, pilot, exclusions and baseline. A sparse policy that exceeds its frame cap stops; it is never silently thinned.
4. **Approve execution plan**. Approval binds a digest of normalized task configuration, provider, source manifest/baseline and selected skill content. Unapproved and legacy pre-Harness runs cannot execute. This approval does not authorize annotation publication.
5. **Run pilot**. Temporal analysis uses coarse observations followed by denser windows around candidate boundaries. Each phase reserves budget and persists its result/usage. A failed/incomplete phase is blocked with previous work retained.
6. Review evidence in the current player, correct labels/bounds/attempt/outcome, and save edits. Unknown/other/background and uncovered intervals are valid. Success requires a short observable evidence statement, not private model reasoning. Multiple attempts need separate attempt numbers. Split/merge in the queue; existing native timeline remains available after publication.
7. Record pilot review notes and accept quality, or reject it. Rejected pilot blocks batch execution. Changed pilot annotations invalidate previous acceptance. **Execute remaining** reuses completed episodes and preserves human corrections.
8. Optional SAM3: for a local object-only task, choose **Local vision tools (no model)** without authorizing media egress, approve the plan and use **Prepare object evidence without model calls**. This uses no language model. Then explicitly plan/run its bounded tool using an existing checkpoint. Inspect persisted masks, use track review, and select **Preview draft masks in current player** on the matching dataset/episode. Drafts are visibly identified. Object visibility, opacity and contour controls change display only. **Show published masks** exits preview. No unseen/absent mask is fabricated.
9. Validate, approve the exact annotation revision, then commit. **Export full dataset with reviewed changes** calls the native exporter into a new dataset-named timestamp directory. This is a full native-dataset export, including unselected episodes unchanged, with the approved annotations; it is not a filtered dataset export. It copies videos independently, and checks source integrity, provenance and lossless object identity/time/masks on re-read. Read `levi-roundtrip-report.json` in that directory.
10. Pause/cancel at safe boundaries. Resume uses structured state rather than chat replay. `plans.rebudget` can revise limits after stopping while retaining completed shards; it revokes execution approval. Other scope/model/semantic changes currently require a new plan, rather than risking an incorrect partial invalidation.

## Runtime contracts and state

`plans.clarify → runs.plan → plans.approve → runs.execute(pilot=true) → plans.review_pilot → runs.resume(pilot=false) → changes.validate/review/edit → changes.approve → changes.commit → export.run`

The capability registry is shared by REST, UI and stdio MCP. External agents can propose but cannot approve their own plan, pilot or commit. Evidence is resolved through authenticated artifacts; `evidence.read` returns bounded pages and MCP resolves real image payloads. IDs alone are not presented as model-visible images.

- **Workflow v1**: task kind; subtask definitions; exclusive/overlapping layer policy; coarse observation step; refinement radius/tolerance; evidence-frame cap; object concepts/visible-mask convention; selected pilot.
- **Plan v1**: revision, normalized digest, human approval, input/annotation baseline, selected skill hashes, pilot decision, exclusions and explicitly uncertain estimate.
- **Temporal proposal**: episode, semantic subtask, attempt, layer, outcome, `[start,end)` or point event, evidence IDs/note, uncertainty, candidate boundaries. Subtask outcome does not imply whole-episode outcome.
- **Evidence**: dataset frame/time, camera, source-ledger mapping, requested/decoded video time and frame, mapping error, native size and content digest. ffprobe frame PTS is used rather than assumed FPS; OpenCV performs CPU decode. Unsupported/missing timestamps block with a clear error.
- **Cache**: lossless frame receipt is keyed by source content, decoded frame and preprocessing; model receipt additionally includes provider/configuration, task policy, selected skills, input manifest and exact evidence. Fingerprints are metadata, never directory suffixes. Initial reuse is within a run, not a global cross-user cache.
- **Observability**: sequenced model-step events include request/token usage, elapsed time and usage provenance. Missing token usage remains a conservative reservation, not zero. Cache hits are recorded; interrupted in-flight billing reservations are retained.

Input snapshot and evidence-artifact byte limits, model call and observation caps are checked before dispatch. Provider token accounting can be reported only after a response: a non-conforming provider's final response can exceed its reservation; LEVI blocks further work rather than claiming to enforce the provider's billing. Real price ceilings require provider-side limits. Cancellation cannot undo an already billed request.

## Validation and staged delivery

1. Contract/approval increment: bypass, stale digest, pilot rejection, changed baseline, external scope and budget tests.
2. Temporal evidence increment: variable PTS mapping, strict coverage cap, coarse/refine fixture, duplicate cache reuse, unknown outcomes and human corrections.
3. Review/export increment: existing-player draft masks, native exporter, exact object re-read and provenance preservation. Browser smoke remains offline/mock-based; real SAM3 visual quality is not measured.
4. Deployment acceptance: genuine visual models, multiple providers, short-event recall, identity switches, occlusion recovery and human review time on a frozen independent reference set. Not executed here.

`agent/evaluation.py` provides temporal identity/attempt/outcome matching with boundary tolerance, RLE mask IoU and an efficiency-comparison guard requiring identical dataset/policy/evidence/validation/output digests. Synthetic tests demonstrate contract behavior only. No token-saving percentage, annotation accuracy or real human-time reduction is claimed without measurements.

The real reference set must include failed/repeated picks, drops/recovery, interruptions, reordered subtasks, short events, occlusion, multiple cameras and VFR. Record per-slice segment/event F1, boundary errors, uncovered/unknown duration, mask IoU, track identity switches, correction propagation, reimport fidelity and timed human edits. Freeze thresholds after pilot, then evaluate a separate set. Compare the same observations and validation in baseline and Harness; missing metrics remain **not measured**.

## Measured run (2026-09-20)

The first end-to-end external-MCP annotation on real data: five episodes of a plate-stacking capture, annotated through the stdio bridge by an external agent, reviewed and committed by a human. Thirty-one subtask segments and 114 per-frame object masks (44 object tracks) were published. What the run changed:

| Observed | Change |
|---|---|
| Evidence density came from a flat sample count, not the approved plan; a 0.5 s coarse step yielded three frames per episode | `runtime.prepare_evidence` goes through the declared observation policy for temporal **and** object work |
| One image per evidence frame dominated the agent's context | `evidence.read` gained `layout: "mosaic"` — one labelled contact sheet per page, tiles still addressable by evidence id |
| Boundary refinement existed only inside the model path | `evidence.refine` exposes the same bounded two-pass refinement to external agents |
| SAM3 died silently with the GPU at 99.7 % used, reporting only a non-zero exit | GPU preflight before launch, exit-code/signal classification with a log path, and `objects.strategy` recommending the agent path when the machine cannot host the worker |
| Object outlines had to be written by a throwaway rig-specific script | `objects.detect` measures candidate regions generically (outline, position, shape, median HSV, one overlay) and `objects.propose` accepts `candidate_id` instead of a returned polygon |
| The second changeset could not be committed because the first had moved the dataset | `changes.rebase` moves a reviewed draft onto the published revision and clears its approval |
| A capability raising inside the API returned an empty body; the client saw a JSON parse error | Readable 500s on the capability route, and a readable failure for any non-JSON response in the terminal client |
| Stale runs blocked their own cleanup forever ("resume or cancel it first") with no way to cancel | `runs.abandon`, and `levi agent clean --abandon` |
| Nothing recorded what the work cost | `runs.report_usage`, `plans.estimate` and `levi agent usage` (see [AGENT_WORKBENCH.md](AGENT_WORKBENCH.md)) |

Measured cost of that run, by the artifacts that crossed the agent's context (image pixel area, JSON bytes; the agent's own reasoning is not visible to LEVI): 94 000 tokens for the temporal work over 208 evidence frames read as 14 contact sheets plus 6 refinement frames, and 32 000 for the object work over 32 frames. Reading those 208 frames singly instead would have cost roughly three times as much by the calibrated model. No percentage saving is claimed beyond these two recorded runs; the estimator states its sample count and widens its range when extrapolating.

## Integration pass (2026-09-20)

After the measured run above, the whole product was exercised end to end against a running instance: browsing every local dataset through the ports the web UI uses, the artifact naming rules, the external MCP toolchain, and an online (API-model) run driven against a local OpenAI-compatible stub so that dispatch, budget, events, artifacts and metering could be checked without paying a provider. What it found:

| Observed | Change |
|---|---|
| Every local dataset answered 401 once the browser was signed in to Hugging Face: the Hub's bearer token was read as a failed Agent credential | A bearer token is claimed as an Agent credential only on the Agent API; elsewhere an unrecognised one is ignored and the UI credential still applies. The Hub token is not sent to LEVI's own service |
| Every local dataset answered 401 while server rendering; the proxy authenticated with a token captured when the frontend started | The token is read from the file the service owns, so a restarted Core does not strand the frontend, and it is attached only to requests aimed at LEVI's own file service |
| A blocked run reported only "Proposal cites unknown evidence" | The message names the citation and the episode whose evidence was supplied |
| A rejected subtask id reported only that it was unknown | The message names the id and lists what the plan defines |
| An interval ending at a float32 episode boundary was reported as an uncovered gap of a millionth of a second | Gaps shorter than a frame are not gaps |
| Stale runs blocked their own cleanup with no way to close them | `runs.abandon`, surfaced as `levi agent clean --abandon` |
| `levi clean` raised a traceback when the service was running, and named neither the process nor a way to stop it | A readable refusal, the PID, and a new `levi stop` |
| Artifacts of a dataset that had left the catalog were invisible | `levi clean` reports them and their dead redirects; empty shells are removed, anything holding review work is only listed |
| Reading a cleaned-up run's evidence raised `FileNotFoundError` | The ledger is returned with what was removed and how to rebuild it; tampered content still fails closed |
| "Regenerable" was not true: cleanup removed the frozen input snapshot too | `runs.prepare` takes the snapshot again from the source and refuses if the source no longer matches |

The online path passed its 22 checks: plan, approval, two model requests, live `model_step` events carrying request count, tokens and elapsed time, an automatically recorded cost sample, evidence artifacts, an artifact manifest, pilot review, remaining episodes, approval and a commit to a timestamped revision. A stub is not a model: it proves LEVI's machinery, not annotation quality.

### Explicit remaining gaps

- No automatic semantic definition-generation before approval; users edit definitions. No real model capability probe or model quality acceptance has been run.
- Dense refinement observes candidate windows; it cannot prove that coarse observations caught every short event. Unseen intervals require human review or a denser revised plan.
- No autonomous keyframe reinitialization, learned identity repair, automatic human-mask propagation, cross-camera identity or amodal masks. Existing manual object edits remain authoritative; track changes need re-review.
- No universal native-video model input, arbitrary external Agent compatibility, public HTTP MCP or native Pi compaction. Optional scoped ACP Pilot integrations are documented in [PILOT.md](PILOT.md).
- Contact sheets reduce cost on the external-MCP path only. An API-model run still receives one image per evidence frame: sending a sheet to a vision model changes what the model sees, and no measurement of that trade-off exists here, so it is not done silently. Online-mode cost is at least measured exactly, since LEVI meters those runs itself.
- Token accounting depends on agents reporting honestly; LEVI marks every sample `self_reported` and can only cross-check it against the scope it measured. Two recorded runs are not a cost model — treat early estimates as order-of-magnitude.
- `objects.detect` measures colour-coherent regions. It has no notion of objects, so a shadow, a table or a human hand can be returned as a candidate; the agent decides, and a human reviews. It is not a substitute for SAM3 on cluttered or textured scenes.
- No cross-run feature cache or general dependency-graph incremental recomputation. Completed shards and identical interrupted phase results are reused; broader changes require new plans.
- Export re-read checks do not certify every downstream training consumer. Rich temporal fields live in the documented LEVI provenance extension and must be read explicitly.
