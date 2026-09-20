# Agent Workbench

LEVI combines bounded Agent proposals, optional SAM3 object assistance and human review. The source dataset stays read-only. Model output is never a human label until an exact ChangeSet is approved and committed.

## Install and start, in order

From the directory containing this README's parent `pyproject.toml`:

```bash
# 1. Install the normal app plus optional Agent SDKs (no model weights).
uv sync --locked --extra agent
bun install --frozen-lockfile

# 2. Choose your workspace. All generated state is relative to this root.
export LEVI_WORKSPACE="$PWD/.state"
mkdir -p "$LEVI_WORKSPACE"

# 3. Build. No API key, GPU, CUDA probe or checkpoint is needed.
bun run build

# 4. Start both services; open the WEB address printed by the launcher.
uv run --extra agent levi
```

The default UI is `http://127.0.0.1:7860`; port 7861 is the backend. This is a local, single-user application, not a multi-tenant server. The launcher generates an internal UI credential; scoped external Agents use a different token. HF login only authorizes HF access.

1. Open **Accounts & connections**. Add a profile with an explicit compatible base URL, model ID and declared image-input capability. Tool-call support is required; a custom base URL does not imply provider capabilities.
2. Supply the API key either through the named server environment variable before launch, or **Session API key** in the account card. Session keys are bound to the exact endpoint/profile, remain in server memory and disappear after restart. Changing provider configuration blocks old runs; create a new plan before sending evidence to the changed service. They are not included in SQLite, exports or run events. **Select**, **Edit**, **Disconnect**, **Reconnect** and **Remove** operate on profiles; disconnect disables new requests but cannot revoke an in-flight request at its provider. Removing a profile does not delete its past run history. HF account switching uses the existing HF menu.
3. Open a registered local dataset or use a Hub ID. In **Agent Workbench**, select a model, explicit episode indices, cameras, budget and instructions. Empty camera scope gives table evidence only. Sending image evidence requires explicit consent. Non-loopback private/metadata addresses are blocked; local endpoints require the loopback opt-in.
4. **Inspect & create plan** fixes the source scope and estimates snapshot bytes. Oversized plans require a smaller scope or explicitly larger storage budget. Remote input is pinned to a Hub commit. Local inputs are independently copied and content-hashed; later source changes block commit.
5. **Approve execution plan**, then **Run pilot** processes one episode with sparse evidence. Inspect its suggestions; After **Accept pilot quality**, **Execute remaining** skips completed episodes and preserves human edits/decisions. Sampling is not a claim of full temporal coverage.
6. Review with **Pending**, **Issues** or annotation-type filters. **J / K** move between suggestions outside text fields. Edit text/bounds, save edits, then accept/reject one item or the visible batch. The evidence image stays next to the review card; optional playback following never navigates away from an unsaved editor. Different episodes must be opened after saving existing edits.
7. **Validate & approve** checks source/evidence/version consistency and explicitly accepts remaining language suggestions. Rejected items stay excluded. **Commit approved changes** atomically activates one version containing language annotations, outcome labels, review flags and accepted object results. Repeat submission returns its receipt; stale drafts cannot overwrite newer data. Reload an already-open legacy editor to see the committed version.
8. **Create undo draft** creates an inverse patch requiring another human approval. Later edits cause a conflict instead of being silently overwritten. A cancelled or crashed run retains evidence, reservations and completed shards. Resume after restoring credentials; if a model budget is exhausted, create a smaller/new plan. **Read completed work** allows review of the completed subset without another model call.
9. Use existing dataset conversion/export controls. Native annotation export includes provenance, native language columns, human outcomes and lossless object sidecars. Raw-capture views must go through conversion; carry-over maps annotations using their source frame ledger. Existing format limitations in [CONVERSION](CONVERSION.md) still apply.

## SAM3 is an optional tool

The Agent workflow works without SAM3. When masks or tracks are needed:

1. Configure its isolated worker and local checkpoint using [SAM3](SAM3.md). This is a separate, explicit setup step.
2. Freeze camera scope in the task. Open **SAM3 · optional object tool**, enter concepts and a bounded frame count, then **Plan object assistance**. Planning only prepares CPU-readable inputs.
3. **Run SAM3 on this scope** is a separate explicit action. It requires an existing, nonempty local checkpoint; this Agent tool never downloads one. Real execution can use the configured CUDA worker. Development tests do not invoke it.
4. Scrub the staged frame slider and inspect native-frame overlays. Accept/reject per track or per camera; relabel concepts when needed. Corrections remain staged and invalidate earlier approval. Masks must all be decided before approval. Refinement is also available through the typed `objects.edit` API using the existing `ObjectEdit` contract.
5. Accepted tracks receive new camera-scoped IDs on commit, preserving already-published objects. Re-running a range may produce duplicate objects: explicitly review/remove duplicates with the object editor, rather than automatically treating similar tracks as the same identity. Cross-camera identity is not inferred.

SAM3 plans/results/checkpoints are separate from model-provider credentials. No VLM-generated mask, simulated score or unmeasured accuracy is presented as SAM3 inference.

## External Agent through MCP (stdio)

Set a shared secret locally in the LEVI server and MCP process environments; do not commit it in client configuration. Choose only the dataset IDs that external Agents may access:

```bash
# Generate once in your private terminal; provide the SAME value to both processes.
export LEVI_AGENT_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export LEVI_AGENT_DATASETS="local/my_dataset"
uv run --extra agent levi
```

The external MCP client's command is `uv`, with arguments `run --project /absolute/path/to/LEVI --extra agent levi agent mcp`, and the same `LEVI_AGENT_TOKEN` plus `LEVI_AGENT_URL=http://127.0.0.1:7861` in its environment. Replace the project path for your clone. The bridge does not obtain HF or provider API keys.

Discover tools, resources and the `review-dataset` prompt. Read the versioned skill resources explicitly; auto-discovery by arbitrary clients is not assumed. A typical sequence is:

```text
workspace.get_context → datasets.inspect → plans.estimate → runs.plan (provider="external")
→ human plans.approve in LEVI → runs.prepare
→ evidence.read (layout="mosaic") → evidence.refine around unclear boundaries
→ annotations.propose_segments / annotations.propose_events → changes.validate
→ human review/approval/commit in LEVI → runs.report_usage
```

Object identity is the part frame-by-frame outlining cannot supply: numbering detections per frame makes every id shift as soon as one object enters or leaves. `objects.propose` takes `track_by: "overlap"` to link the same object across annotated frames, and always reports how many tracks each concept ended up with against how many were ever visible at once — more tracks than instances means an object changed identity. When the annotated frames are too far apart for outlines to overlap, linking refuses and says so instead of splitting one object into a track per frame. `levi agent objects relink --dataset <name>` runs the same check over a published revision.

For object masks the middle of that sequence becomes:

```text
objects.strategy → (SAM3: objects.plan → objects.run)
                 → (agent: objects.detect → objects.propose citing candidate_id)
→ objects.inspect / objects.edit → human review/approval/commit
```

`TaskContext` requires `repo_id`, `episodes`, `instruction`, `provider`; include cameras and explicit `allow_media_egress` when needed. No local model profile is required for `provider="external"`. Tool schemas come from `capabilities.list`. External credentials grant read, draft and bounded local execution on allowlisted datasets only, never human approval/commit or legacy writes. "Execution" here is the isolated SAM3 worker on this machine: it stages results for review and publishes nothing, and it refuses to start when the GPU lacks the headroom it needs (`LEVI_SAM3_MIN_FREE_MIB`, default 7000 MiB) rather than dying mid-run. Accounts & connections displays the configured scope and can disconnect/reconnect external access without exposing the token; the disconnect choice persists in the workspace. SAM3 execution may be triggered by a scoped external connection, but its output is always staged for a human; `objects.strategy` reports whether this machine can run it at all. REST is versioned at `/api/levi/agent/v1`; MCP currently uses stdio, **not Streamable HTTP**. HTTP transport and UI-driven ACP agents remain later work.

## Architecture and extension contracts

| Concern | Entry point | Extension rule |
|---|---|---|
| Dataset evidence | `levi/agent/formats.py:DatasetAdapter` | Register a reader with pin/inspect/snapshot/sample/verify/publication_state; preserve real timestamps, camera scope, native v3 offsets and source hashes. Default reader covers supported LeRobot versions and normalized raw-capture views. |
| Annotation semantics | `AnnotationKind` / `register_annotation` | Register validate/apply/paths together. The same handler validates model/MCP proposals, writes the bundle and determines inverse paths. Preserve `[start,end)` intervals vs point events. New UI rendering/export features need their own tests. |
| Conversion | `levi/conversion/registry.py` | Implement `InputFormat` / `OutputFormat`; use source-frame provenance and annotation carry-over. Never modify the source. |
| Native annotated export | `levi/agent/formats.py:ExportAdapter`, `backend/app.py:_do_export` | Read the active bundle through the shared resolver; record Agent provenance. Do not read a stale legacy sidecar path. |
| Model calls | `levi/agent/providers.py:ModelProvider` | Explicit capability/configuration and typed outputs; no tool permissions inferred from prompts. |
| Business tools | `levi/agent/capabilities.py:SPECS` | Single schema/permission/side-effect registry for UI, REST and MCP; tools reuse services rather than bypass them. |
| Optional object execution | `levi/agent/objects.py` | Isolated existing worker, staged results, source-ledger validation and human review. |
| Storage | `levi/agent/store.py` | Immutable directories plus one SQLite transaction for the active bundle pointer and idempotency receipt. |

Legacy language/outcome/object/review writers join the bundle transaction after first activation. They require the revision originally observed by the editor; new Agent work cannot silently replace unsaved human work. Read-only snapshots do not import/migrate annotations or mutate originals.

## The plan form

The form offers what the dataset declares rather than asking you to type it:

| Field | What it means |
| --- | --- |
| **Tasks** | Shown only when the dataset declares more than one. A note for the reader; the frozen scope is the episodes. |
| **Episodes** | Click one, or drag across a run of them. Frozen at approval — it cannot grow later. |
| **Cameras** | Only the chosen cameras are read. Subtask and object work needs at least one. |
| **Task instructions** | What to look for and how to judge it, given to the agent with the evidence. |
| **Task type** | Dataset review, video subtasks and events, or visible object masks. |
| **Working mode** | *Produce annotations* — it proposes, you review. *Read only* — it may look but not propose. |

Spending limits appear only for a configured model endpoint, where they cap what LEVI itself spends. On the MCP channel your agent reads through its own connection and spends its own tokens, so LEVI sets no limit; the connection's dataset scope is the consent that a model profile asks for with a checkbox.

## Handing a task to an external agent

Nothing starts by itself on the external MCP channel: approving a plan unlocks it, and the agent does the work in its own context. The sequence is

1. Plan the task in **Tasks & review** and approve it.
2. Tell your agent to pick up the latest run. It calls `runs.list`, which shows every run in its dataset scope and what each is waiting for — `agent_prepare` and `agent_propose` are its turn, everything else is yours.
3. Watch it in **Live activity**: one card per task with its progress, plus the stream of what the agent is doing.
4. Review and commit in LEVI. The completion notice carries the revision's path and a link to the result.

`runs.list` reports `evidence_ready` because cleanup frees evidence that `runs.prepare` can rebuild; a run whose frames were removed says so rather than looking ready to read.

## Watching an agent work

Open **Agent Workbench → Live activity**. Everything an agent does passes through one dispatcher, so the panel shows every channel without a second audit path: the action in words, the dataset and episode, how long it took, and the reason when something is refused. A call that fails authorization appears too — a silent permission error is the hardest kind to diagnose.

Artifacts land in the same panel. A commit, an export or a cleanup contributes its path, and clicking one copies it, which is usually the next thing a person needs.

The stream is Server-Sent Events at `/api/levi/agent/v1/activity/stream`, with `/api/levi/agent/v1/activity` for the first paint. Both require the operator credential the web UI holds: an agent can act, but it cannot read the workspace's record of what every agent has been doing. Discovery and polling calls (`capabilities.list`, `runs.get`, `objects.frame`, …) are left out unless they fail, so a busy agent stays readable.

### Sparse masks in the viewer

Objects are annotated on sampled frames. The playback bar marks those frames and steps between them, and between them each track's last measured outline is carried forward, dashed and labelled with its source frame. A dashed outline is a measurement from another frame, never a claim about the current one; the solid, filled mask is the frame that was actually annotated.

## Cost of an annotation run

LEVI cannot meter an external agent's context — Codex, Claude Code or any MCP client spends its own tokens outside this process. What LEVI can do is measure the scope, price it, and remember what each agent actually spent.

- `plans.estimate` prices a scope before you commit to it. It returns a range with its basis stated, never a price, and keeps a run's fixed cost separate from its per-episode and per-frame cost. With no history it uses published defaults and says so; after the first `runs.report_usage` it calibrates to that agent. Scopes far larger than anything recorded are marked as extrapolations.
- `runs.report_usage` is how an agent closes the loop: it reports its own token use, LEVI attaches what it measured (episodes, evidence frames, whether they were read as sheets or singly) and stores the pair. Estimates for every later run improve from it. A rough figure is worth more than none.
- Samples are grouped per agent — a mosaic-reading MCP client and a single-frame API model do not cost the same per frame — and `levi agent usage show` prints the per-agent history.

The largest lever is how evidence is read. `evidence.read` with `layout: "mosaic"` returns one labelled contact sheet per page instead of one image per frame, and every tile still maps back to its evidence id, so citations survive the packing. Read coarsely, then call `evidence.refine` only around the boundaries you could not resolve.

## Cleaning up

`levi agent clean` previews what a cleanup would remove; `--apply` performs it after an explicit terminal confirmation. It deletes only what can be regenerated from the dataset — frozen input snapshots, evidence images and contact sheets of runs that have ended — and keeps committed revisions, their provenance and inverse patches, the evidence ledger, and the staged drafts of runs that are still open. A run nobody will finish is closed with `--abandon <run-id>` first, which refuses if that run has anything committed or approved.

Reading a cleaned-up run's evidence still works: the ledger comes back with `images_removed` and a note saying the frames can be rebuilt with `runs.prepare`, which takes the snapshot again from the untouched source and refuses if that source no longer matches what the run was approved against. Evidence whose content changed still fails closed as tampering.

`levi clean` is the separate, workspace-wide cleanup: regenerable caches, plus a report of artifacts and redirects left behind by datasets that are no longer registered. It requires the service to be stopped (`levi stop`).

## Readable workspace layout

Paths below are relative to `LEVI_WORKSPACE`; `<dataset-name>` is the same catalog name used everywhere else. No fingerprint is used as a directory suffix.

```text
outputs/LEVI/workbench/
  agent/workbench.sqlite3                 # tasks, events, heads, receipts, leases
  agent/datasets/<dataset-name>/
    runs/<UTC-timestamp>/
      input/                             # independent bounded snapshot
      input-manifest.json                # content hashes live here, not in names
      evidence/episode_000000--<camera>--frame_000010.png
      objects/<UTC-timestamp>/draft/      # staged Parquet + COCO RLE revisions
    revisions/<UTC-timestamp>/
      annotations/episode_000000.json
      annotations/outcomes/episode_000000.json
      object_annotations/
      review.json
      agent-provenance.json
      inverse/
outputs/LEVI/exports/<dataset-name>_annotated/
```

Pre-Agent sidecars remain in their existing named locations until activation. Old versions remain for audit/undo; there is no automatic destructive migration or retention purge. Back up the complete `outputs/LEVI/workbench` tree with services stopped, not just SQLite or just active folders. Move the whole workspace, set `LEVI_WORKSPACE` and register source paths at the destination; restore server credentials separately. Runs with missing inputs or session credentials must remain blocked rather than use another dataset. Snapshots can be large: select a small scope first.

Hub caches now use `<org>__<dataset>/account-0001/revision-0001/` with a metadata index. Accounts remain isolated; older digest-named caches are not deleted, shared or required for new runs. Dependency managers may maintain their own opaque package-cache internals; LEVI's dataset/task/output naming does not use those names.

## Validation and limitations

```bash
uv sync --locked --extra agent
bun install --frozen-lockfile
uv run --extra agent pytest --basetemp="$LEVI_WORKSPACE/tmp/build/pytest"
bun run format
bun run validate
bun run build
git diff --check
```

Automated tests use fixed data, mocked model responses and protocol substitutes. They do not run CUDA checks, real model inference or checkpoint downloads. CPU video decoding is used for media evidence. Actual provider capabilities, annotation quality, a real MCP client and real SAM3 inference require separate manual acceptance; no accuracy claim follows from fixture tests.

Not yet supported: ACP-driven external sessions, Streamable HTTP MCP, multi-agent collaboration, cross-camera identity inference, visual workflow graphs, autonomous commit, full-scene event inference or a public multi-tenant deployment. Pause/cancel is cooperative at safe boundaries; in-flight model billing cannot be undone. SAM3 resumes at a new bounded job, not mid-tracker memory. A sampled run must never be labelled full-frame coverage.


## Acceptance record

| Requirement | Evidence / status |
|---|---|
| AC-01, AC-02 | Typed compatible-adapter contract exercised through the installed SDK and a mock HTTP transport; real visual model and two real endpoints still require manual acceptance. |
| AC-03 | Official MCP SDK in-memory discovery/tool/resource/prompt roundtrip is automated; a separately deployed external Agent client still requires manual acceptance. |
| AC-04 | Deferred: RuntimeAdapter protocol reserved; no ACP driver shipped. |
| AC-05 | Worker/checkpoint preflight is filesystem-only and blocks unavailable SAM3. GPU availability/inference is intentionally not probed by these tests. |
| AC-06 | Browser test retains unsaved draft edits across polling; legacy writers require the observed version after activation. |
| AC-07, AC-08 | Scope, interval, evidence ledger and staged object review tests; sample-based suggestions do not establish full-episode annotation quality. |
| AC-09, AC-10, AC-12 | Source-content checks, duplicate receipts, stale-version rejection and fault-injected atomic publication tests. |
| AC-11, AC-13 | Lease recovery, reserved budgets, cancellation boundaries and scope-denial tests; abrupt real worker/network failure remains a deployment acceptance case. |
| AC-14 | Model can only propose structured data; read-only model tool and server-side capability enforcement. Egress endpoints are resolved/pinned; credentials never enter prompts. No blanket adversarial robustness claim. |
| AC-15 | Not executed: needs a frozen, independently human-reviewed quality set. |
| AC-16 | Existing native export/carry-over regression plus shared active-bundle resolver; no new dataset format support is implied. |
| AC-17 | UI/REST/MCP invoke the same capability service; browser smoke and SDK roundtrip are fixture tests. |

Passing the engineering checks does not by itself establish real-model annotation quality, full coverage or external-runtime support; those are measured separately and are listed as open in the [validation record](VALIDATION.md).


The newer [Harness guide](HARNESS.md) specifies executable plan approval, pilot gates, temporal definitions, evidence caching, native player drafts and verified export. Earlier plans without a bound approval must be recreated.

## Codex / Claude Pilot

[Headless and online Pilot](PILOT.md) extends this same Harness. New connections use scoped grants and an on-demand shared core; legacy environment-token connections remain supported.
