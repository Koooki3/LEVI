# Changelog

## Unreleased — Live agent activity

- **The plan form offers what the dataset declares.** Episodes, cameras and tasks are chosen by clicking or dragging across chips, with select-all, instead of being typed as comma-separated text where a typo only surfaced as a failed plan. Ambiguous options carry a line of explanation, "Annotation workflow" is now "Task type", and "Draft suggestions" is "Produce annotations".
- On the MCP channel the call and token limits are gone — your agent spends its own tokens — and the media-egress checkbox with them: the scoped connection's dataset list is that consent. Both remain for a configured model endpoint, under a disclosure that says what they cap.
- Identifiers stop at the minute (`temporal-20260920T0926`); finer digits read as an opaque suffix. The disambiguating suffix on a name clash keeps seconds, because separating two names is its only job.

- **The plan form offers what the dataset contains.** Episodes, cameras and — for a multi-task dataset — tasks are chosen by clicking, dragging across a run, or selecting all, instead of typed into a comma-separated box where a typo only surfaced as a failed plan. "Annotation workflow" is now "Task type", the working mode says "Produce annotations", and the ambiguous options carry a line saying what they actually do.
- **No call or token limit on the MCP channel.** Those caps only ever constrained what LEVI itself spends at a model endpoint, which is nothing when your own agent does the work; they now appear only for a configured model, under "Spending limits", with the media-egress consent that belongs to them. On the MCP channel the connection you created — which already names the datasets it may touch — is the consent.
- The task instruction field shows a worked example as its placeholder, and a finished external run with no reported usage says so, since the cost model only improves when someone notices.

- **Identifiers stop at the minute.** Run, revision, changeset and job ids were `…T092616424974`; the sub-minute tail read as an opaque suffix and told nobody anything. They are now `temporal-20260920T0926`, with `-2`, `-3`… only when two land in the same minute. Every id generator checks for that clash — an unchecked one let an undo overwrite the very changeset it was undoing. The workspace now has one timestamp vocabulary: a second generator with its own precision produced ids that no id check recognised.
- **A local connection lasts until it is disconnected.** `levi agent connect` no longer imposes an expiry or a call cap by default; pass `--hours` or `--max-calls` for a deliberately bounded one. Disconnecting still stops it immediately, and the credential stops authenticating.
- **The connection card distinguishes configured from live.** LEVI cannot detect an MCP client that has not spoken to it, so a connection reads "waiting for the agent to call" until its client calls this LEVI instance, then "connected" with the time of its last call. A new Codex or Claude Code thread reusing the project's configuration turns it live on its first call, with no re-connect step.
- The Agent panel can be widened by dragging its left edge, with arrow keys as the keyboard equivalent; the width is kept per browser.

- **A run is named for the work it does.** Run and revision ids carry the task kind — `temporal-20260920T…`, `objects-20260920T…` — under the dataset's own directory, so a path says which dataset, which kind of task and when, and never a digest.
- **One call orients a new agent.** `workspace.get_context` now returns what it may do, what only a person may do, the order to work in, the runs already waiting for it, and the habit that decides what a task costs. An MCP client connecting for the first time can work without reading the source.
- Task cards and the completion notice carry the token count, and say whether LEVI metered it or the agent reported it.
- LEVI no longer describes itself as experimental. The panel states the contract that matters — evidence is sampled and every suggestion is reviewed by a person — rather than a disclaimer about maturity; the documents keep the specific, measured limits instead.

- **An agent can find the work waiting for it.** `runs.list` returns the runs inside a connection's dataset scope and says whose turn each one is: your approval, the agent's evidence, the agent's suggestions, your review, your commit, or nothing. Plan and approve in the workbench, then tell the agent to pick up the latest run — no run id to copy across.
- Listing respects cleanup: a run whose evidence was freed reports `evidence_ready: false` and `evidence_cleaned: true`, so an agent rebuilds the frames with `runs.prepare` instead of reading files that are no longer there, and finished work is left out of the list unless asked for.
- **The live panel tracks tasks, not just calls.** Each run appears as a card with its progress, what it is waiting for and its last action; selecting one filters the stream to that task. A task that commits raises a completion notice with its artifact path and a link straight to the result.

- **Object masks follow the video.** They are annotated on sampled frames, so between them the overlay had nothing to draw and an object appeared to vanish. A mask outside its own frame is now carried from the nearest annotated frame within the same track's span, drawn dashed and dimmed and labelled with the frame it came from — visible continuity without claiming a position nobody measured.
- **The playback bar marks annotated frames** and steps between them, so masks can be found instead of scrubbed for.
- **The Codex / Claude panel says what it checked.** It reported LEVI's pinned adapter version as though it were the installed one, called the adapter's absence "not installed" without saying which thing, and printed a constant "unknown" login status — so "Refresh status" could never change anything. It now reports the runtime's own CLI on this machine and LEVI's adapter separately, and states that login belongs to the official client rather than implying a failed probe.

- **A live activity panel.** Every capability call, through the web UI, the REST API, the MCP bridge or a managed Pilot, is recorded in one journal and streamed to the workbench as it happens: what the agent did, to which dataset and episode, how long it took, and why it was refused. Refusals during authorization are recorded too, so a permission error is visible rather than silent.
- **Artifact paths are offered for copying.** A commit, an export or a cleanup puts its path in the panel, which is what gets pasted into the next command.
- **The external Agent card tells the truth.** Connection state was read only from this process's environment, so a live scoped MCP connection showed as disconnected with no dataset scope. It now reports the actual grants, their scope, call count and remaining validity, and refreshes while the panel is open.
- Object annotation gained the identity step that frame-by-frame outlining cannot do for itself: `objects.propose` can link the same object across annotated frames by overlap (`track_by: "overlap"`), reports how many tracks each concept has against how many were ever visible at once, and refuses to link when the annotated frames are too far apart for overlap to mean anything. `levi agent objects relink` applies the same check to a published revision.

## Unreleased — Integration pass

- **Object masks are drawn again.** Masks are stored as flat `rle_size`/`rle_counts` parquet columns; every reader converts them to the `mask_rle` the shared model declares, except the endpoint serving the viewer, which shipped the raw columns to the browser. The overlay found no mask and threw on `mask_rle.size`. The conversion now happens once, inside the reader, and the five hand-written copies of it are gone.
- **Signing in to Hugging Face no longer breaks local datasets.** A signed-in browser attaches its Hub bearer token to every dataset request; LEVI read any bearer token as a failed Agent credential and answered 401, so each local dataset stopped loading. A bearer token is now only claimed to be an Agent credential on the Agent API, where an invalid one is still rejected and a valid one is still confined to its scoped capabilities. The Hub credential is also no longer sent to LEVI's own file service at all.
- Local datasets load during server rendering again: those reads go straight to the loopback API, which requires LEVI's own token, and the browser's proxy reads that token from the file the service owns instead of a copy captured when the frontend started. A restarted Core no longer strands the web UI with 401s on every local dataset.
- LEVI's internal token is attached only to requests aimed at LEVI's own file service, never to a Hugging Face request.
- `levi stop` stops the shared service; `levi --help` lists the subcommands it dispatches before parsing (`stop`, `clean`, `migrate`, `convert`, `agent`, `sam3`).
- `levi clean` refuses readably instead of raising a traceback, names the process holding the service and how to stop it, and survives a corrupt `server.pid`.
- `levi clean` reports artifacts left behind by datasets that are no longer registered, and the redirects that now lead nowhere: empty directories are removed, anything holding review work is only listed.
- The Agent panel separates "Accounts & connections" from "Model settings" into their own tabs, and the header entry for each opens its own.
- Errors an agent has to act on now name the thing that failed: which subtask identity was rejected and what the plan defines, and which evidence id was not in scope.
- Coverage no longer reports a gap of a millionth of a second where an interval ends at a float32 episode boundary.
- A dataset that cannot be read says which dataset, where it lives and what the status means; the two bundled demo datasets had been removed upstream and are replaced with public LeRobot datasets checked reachable on 2026-09-20.

## Unreleased — Agent cost, evidence packing and cleanup

- Evidence follows the approved observation policy for temporal and object work, can be read as one labelled contact sheet per page (`evidence.read layout="mosaic"`), and refined locally around unresolved boundaries (`evidence.refine`).
- Object annotation adapts to the machine: `objects.strategy` recommends SAM3 or the agent itself from worker, checkpoint and GPU headroom; SAM3 refuses to start without headroom and reports why when it dies; `objects.detect` measures candidate regions with no model or GPU and `objects.propose` accepts a `candidate_id` in place of a returned outline.
- Token accounting per agent: `runs.report_usage` records what an agent spent beside what LEVI measured, `plans.estimate` prices a scope from that history with its basis and extrapolation stated, `levi agent usage` shows both.
- `changes.rebase` moves a reviewed draft onto a newly published revision instead of forcing the work to be redone.
- Built-in cleanup: `levi agent clean` removes only regenerable snapshots, evidence and sheets of finished runs, `--abandon` closes runs nobody will finish, and committed revisions, provenance, inverse patches and open drafts are kept.
- Readable failures from the terminal client and the capability API instead of empty response bodies.
- An API-model run records its own measured cost when it finishes, so estimates for online mode come from LEVI's own metering rather than a self-report.
- The Agent panel separates "Accounts & connections" from "Model settings"; the header entry for each now opens its own tab.
- A dataset that cannot be read says which dataset, where it lives and what the status means: the Hub answers 401 for both private and removed repositories, which read as a LEVI login failure. The two bundled demo datasets had been removed upstream and are replaced with public LeRobot datasets checked reachable on 2026-09-20.

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
