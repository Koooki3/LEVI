# Agent Workbench (experimental)

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
workspace.get_context → datasets.inspect → runs.plan (provider="external")
→ human plans.approve in LEVI → runs.prepare → media.sample (includes actual MCP image content)
→ annotations.propose_segments / annotations.propose_events → changes.validate
→ human review/approval/commit in LEVI
```

`TaskContext` requires `repo_id`, `episodes`, `instruction`, `provider`; include cameras and explicit `allow_media_egress` when needed. No local model profile is required for `provider="external"`. Tool schemas come from `capabilities.list`. External credentials grant read/draft on allowlisted datasets only, never human approval/commit or legacy writes. Accounts & connections displays the configured scope and can disconnect/reconnect external access without exposing the token; the disconnect choice persists in the workspace. SAM3 execution remains a human action. REST is versioned at `/api/levi/agent/v1`; MCP currently uses stdio, **not Streamable HTTP**. HTTP transport and UI-driven ACP agents remain later work.

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

The change is intentionally experimental. Passing engineering checks does not mean all acceptance requirements, real-model quality, full coverage or external-runtime support have passed.


The newer [Harness guide](HARNESS.md) specifies executable plan approval, pilot gates, temporal definitions, evidence caching, native player drafts and verified export. Earlier plans without a bound approval must be recreated.

## Codex / Claude Pilot

[Headless and online Pilot](PILOT.md) extends this same Harness. New connections use scoped grants and an on-demand shared core; legacy environment-token connections remain supported.
