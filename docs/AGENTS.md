# Agents and the harness

LEVI lets a model do the looking and keeps the judgement with a person. An agent can plan evidence, read it, and propose annotations; only a person approves a plan, accepts a pilot, and publishes a result. That boundary is enforced in one capability layer shared by the web UI, the REST API, the MCP bridge and the CLI, not by convention.

This guide covers how to drive an agent, what a task goes through, and the **harness** around it: the task ledger, measured cost, local memory, the self-improvement loop and teacher supervision.

- [Channels](#channels)
- [Install and connect](#install-and-connect)
- [A task, end to end](#a-task-end-to-end)
- [Natural-language tasks](#natural-language-tasks)
- [Evidence and refinement](#evidence-and-refinement)
- [Object masks](#object-masks)
- [The harness](#the-harness)
- [Watching and cleaning up](#watching-and-cleaning-up)
- [Capability reference](#capability-reference)
- [Contracts for extension](#contracts-for-extension)
- [Limits](#limits)

## Channels

The channels differ in who runs the model and therefore who spends the tokens.

| Channel | Where the model runs | Tokens | Set up with |
| --- | --- | --- | --- |
| **External MCP** | Your own agent (Claude Code, Codex, any MCP client) | Your agent's context; it reports its use, LEVI measures what it sent | `levi agent connect` |
| **Online model** | Inside LEVI, against an OpenAI-compatible endpoint | Metered by LEVI on every request; the key comes from the environment variable the profile names (`key_env`, default `LEVI_MODEL_API_KEY`) and is never stored | Agent Workbench → Accounts & connections |
| **Local model** | Inside LEVI, against a local Ollama model (default `qwen3.5:4b`) | Metered by LEVI; no API key, no cloud | [Local models](OLLAMA.md) |
| **Managed Pilot** | A Codex or Claude Code session LEVI supervises | That session | [Pilot](PILOT.md) |

All four go through the same plan, evidence, validation, review and commit path.

## Install and connect

```bash
uv sync --locked --extra agent
uv run --extra agent levi build
uv run --extra agent levi            # Web UI on http://127.0.0.1:7860
```

Give an external agent a scoped connection. The command previews the MCP entry it will add to the project, then writes it with `--apply`:

```bash
uv run levi agent connect --client claude --project /path/to/agent/project \
  --dataset local/my_dataset --apply
uv run levi agent disconnect <grant-id>    # revokes it and removes that MCP entry
```

A connection can read and draft on its datasets only; it can never approve, commit, reset or clean. Its credential lives under `outputs/LEVI/workbench/agent/connections/<client>-<timestamp>/`. The older shared-secret setup (`LEVI_AGENT_TOKEN` + `LEVI_AGENT_DATASETS`) still works.

A first-time agent should call `workspace.get_context`: it returns what the agent may do, what only a person may do, the order to work in, and the runs already waiting for it.

An agent that works from a shell rather than through MCP calls the same capabilities with its connection's credential (`LEVI_AGENT_GRANT_FILE`):

```bash
uv run levi agent call evidence.read '{"run_id": "…", "episode": 3, "layout": "mosaic"}'
uv run levi agent call annotations.propose_segments @- < proposals.json   # or @file.json
```

It prints the JSON answer on one line, then an `IMAGE: <path>` line for each picture the answer carries (saved under `<workspace>/tmp/agent-images/<run>/`, or `--images DIR`) — what the MCP bridge sends as image content.

## A task, end to end

```text
runs.plan → plans.approve → runs.prepare / runs.execute (pilot) → plans.review_pilot
→ runs.prepare / runs.resume (remaining) → changes.validate → changes.review → changes.commit
```

1. **Plan.** In **Agent Workbench → Tasks & review**, choose episodes (click or drag), cameras, a task type — dataset review, video subtasks and events, or visible object masks — and instructions. For subtask work, write each subtask's definition: when it starts, when it ends, what counts as success. Those definitions are the contract the agent annotates against.
2. **Approve.** Approval freezes the scope: episodes, cameras, instructions, definitions, the harness parameters in force, and a digest of the source files. Nothing widens later. Approving publishes nothing.
3. **Pilot.** One episode first. On the MCP channel the agent picks the run up itself: `runs.list` shows every run in its scope and whose turn it is — `human_approval → agent_prepare → agent_propose → human_review → human_commit`. A person reviews the pilot and accepts or rejects it; a rejected pilot blocks the rest.
4. **Remaining episodes.** Completed episodes are never redone; human edits are kept.
5. **Review and commit.** The queue shows each suggestion with the frames it cites. Accept, reject or edit; an unknown outcome is never quietly turned into a label. Commit publishes one immutable revision with provenance and an inverse patch. `changes.undo` drafts the inverse for the same review; `changes.rebase` moves a reviewed draft onto a newer published revision.

Re-annotating an episode replaces the segments a previous agent run published there, unless a person edited them since; those are kept. Each published segment carries its subtask, outcome, attempt, stated uncertainty and the run it came from; this LEVI-only metadata is never written into the exported LeRobot columns.

## Natural-language tasks

With a bound local model, a sentence can start a task:

```bash
uv run levi agent task new "Check the quality of stack_the_plates_of_same_color_together, \
  then annotate subtasks on the first 10 demos and report tokens and time" --provider qwen-local
uv run levi agent task approve <task-id>     # after reading the checked spec
uv run levi agent task advance <task-id>     # runs the next automatic step, stops at each human gate
uv run levi agent task show <task-id>
```

The same console sits at the top of **Agent Workbench → Tasks & review**. The local model only proposes a structured spec (dataset, ordered `quality` / `annotate` steps, cameras, definitions, what to report). LEVI checks every field against the catalog — dataset exists, episodes in range, cameras real, instruction present — and a person approves before anything runs. Plan approval, pilot review and commit remain separate human gates. When the task finishes, it reports total tokens and time per step. Records: `outputs/LEVI/datasets/<name>/tasks/task-<timestamp>/task.json`.

## Evidence and refinement

Evidence is what a task costs, so it is read deliberately:

- `evidence.read` with `layout: "mosaic"` returns one labelled contact sheet per page instead of one image per frame; a page holds up to 32 frames by default, so a coarse pass is usually one call. Every tile keeps its evidence id. `images: false` returns the ids and times only, for citing frames already seen.
- `evidence.refine` adds dense frames only around moments the coarse pass could not resolve, within the plan's window and frame cap, and answers with one JPEG sheet of the added frames of the call (six to a row, split past 30) and one summary line per instant. Refinement samples at the plan's boundary tolerance. When the cap cannot take every requested instant, the ones that fit are refined and the rest are listed in `skipped_around_seconds`.
- `annotations.propose_segments` takes several episodes per call. An external agent may leave `evidence_ids` out (LEVI cites the observed frames inside each interval), a `success` uses its content as the evidence note unless one is given, and a boundary within half a frame of the episode's first or last frame is snapped to it. A model's own answers are not completed this way: they must cite their evidence.
- `evidence.changes` ranks the coarse intervals by how much the picture changes across them. When the dataset has a published `evidence.refine_top_k`, `runs.prepare` returns `refine_first` — the instants to refine before proposing — and the in-process runtime refines them too.
- Coverage is reported as sampled, never full; an interval nobody looked at densely is marked uncertain, not failed.

## Object masks

`objects.strategy` reads the machine — SAM3 worker, checkpoint, free GPU memory — and recommends a path. With SAM3: `objects.plan → objects.run`. Without it: `objects.detect` measures candidate regions with no model or GPU, and the agent submits the ones that are objects by `candidate_id` through `objects.propose` (`track_by: "overlap"` links identities across frames and refuses when frames are too far apart). Both end in the same staged review. From a terminal: `levi agent objects status|run|review|show|relink …`. See [SAM3](SAM3.md).

## The harness

Everything a task leaves behind is kept so the next task on the same dataset starts better. The harness makes no model call of its own.

### Closing a task

Every run that ends — committed, cancelled or failed — is closed exactly once, and a service restart closes any that ended unnoticed. Closing writes:

| Record | Path (under `outputs/LEVI/`) |
| --- | --- |
| Task ledger — scope, per-tool calls, time and response size, tokens, quality warnings per episode, published revision | `datasets/<name>/tasks/<run>/ledger.json` |
| Dataset memory — only human-committed facts: segments per `episode_NNNNNN`, subtask and outcome statistics, recurring uncertainty, definitions, lessons, cost profiles | `workbench/memory/<name>.json` |
| Improvement candidates | `workbench/improvements/<name>/<slug>.json` |

A new run freezes a snapshot of the dataset's memory and harness parameters at plan time (`run.harness`), so knowledge gained while it works reaches the next run, not this one.

### Cost

Each closed run gets one token and time record, whatever ran it:

- `tokens.source` says which figure is used: `metered` (LEVI ran the model), `reported` (an external agent's own account), or `delivered_lower_bound` (what LEVI measured sending to the agent). The web UI's own calls are listed separately and never counted as tokens.
- Time is split into wall, LEVI tool time, model time and the agent's side.
- A breakdown names what the tokens went to (evidence images, a tool's text) and flags waste such as evidence pages read twice.

Records fold into `cost_profiles` per agent — `model:<id>`, `external:<name>`, `pilot:<runtime>` — so a change of model or harness parameter shows up against *this dataset's* history. `plans.estimate` adds a `local` estimate from that history; `cost.profile` and `levi agent usage show` print it.

### Self-improvement

Closing a run triages it deterministically. A recurring pattern files an improvement candidate — one file per candidate, named for what it changes:

```text
proposed → evaluating → qualified → awaiting_authorization → published → observing → retained | rolled_back
```

- Only listed harness parameters can be published, within bounds: `evidence.refine_top_k` (refine the most-changed intervals first) and `evidence.mosaic_tile_width` (sheet size, proposed only down to a width reviewers have already accepted on this dataset). Validators, permissions and held-out data are out of reach.
- `improvements.evaluate` has LEVI compute the evaluation on stored evidence; a candidate may be revised once and must pass again. An agent can move a candidate up to `awaiting_authorization`; publishing, retaining and rolling back take a person: `levi agent improvements publish|retain|rollback <slug> --dataset <id>`.
- Cost rules file `cost-rise-*` when an agent gets markedly more expensive on a dataset, `slow-*` for slow tools, and `oversized-*-responses` as software reports. Software candidates are resolved by a developer, never published.

### Teacher supervision and teaching memory

A run can put a **teacher** — a scoped external connection — in front of each learner phase (`supervision: supervised`). The teacher reads the phase with `supervision.pending`, then accepts, revises (with a corrected output) or rejects it through `supervision.feedback`. Feedback never grants plan, pilot or commit authority.

Each piece of feedback is kept twice: as a file, `datasets/<name>/teaching/<run>/episode_NNNNNN-<phase>.json`, and as a note in the dataset's memory. A model LEVI runs receives a bounded brief in its prompt — dataset profile, subtask vocabulary, recurring uncertainty, the teacher's notes and examples from **other** episodes, never the ones it is annotating. Notes about using LEVI itself (for example how to read a request) live in `workbench/memory/workspace.json`.

A teacher's committed work can be frozen as a reference (`datasets/<name>/teaching/reference-<task>.json`) and a learner run graded against it: segment F1, outcome accuracy, boundary error and key-event agreement for temporal work; task identity and outcome accuracy for episode review. Grades accumulate in `teaching/grades.json`.

## Watching and cleaning up

**Live activity** streams every action as it happens: what was done, to which dataset and episode, how long it took, and why a call was refused. Each run is a card with its progress and what it waits for. The stream is Server-Sent Events at `/api/levi/agent/v1/activity/stream` and needs the operator credential; an agent cannot read it.

```bash
uv run levi agent clean                     # preview; --apply frees what runs.prepare can rebuild
uv run levi agent clean --abandon <run-id>  # close a run nobody will finish
uv run levi agent reset --dataset <id>      # remove one dataset's agent history (preview, then --apply)
uv run levi agent memory show|search|rebuild --dataset <id>
uv run levi agent knowledge list|refresh|promote|reject      # built-in knowledge, see Knowledge.md
```

`clean` removes input snapshots, evidence images and contact sheets of finished runs and keeps revisions, provenance, inverse patches, the evidence ledger and open drafts. `reset` removes the record of the work itself for one dataset: runs and their records (teaching phases and cached model answers included), tasks, task ledgers, teaching files, the dataset's memory and its improvement candidates. It keeps quality reports, workspace-wide memory, connections and provider settings. Knowledge worth keeping beyond one dataset belongs in [built-in knowledge](KNOWLEDGE.md).

## Capability reference

`GET /api/levi/agent/v1/capabilities` (or `capabilities.list`) returns each capability's schema, permission and side effects. MCP tool names use `__` for `.`.

| Group | Capabilities | Who |
| --- | --- | --- |
| Orientation | `workspace.get_context`, `capabilities.list`, `datasets.inspect`, `episodes.query` | agent |
| Quality | `quality.inspect` | agent |
| Planning | `plans.clarify`, `plans.estimate`, `runs.plan`, `runs.list`, `runs.get` | agent |
| | `plans.approve`, `plans.review_pilot`, `plans.rebudget` | **person** |
| Execution | `runs.prepare`, `runs.execute`, `runs.resume`, `runs.pause`, `runs.cancel`, `runs.finish`, `runs.events`, `runs.result`, `runs.quality`, `runs.report_usage` | agent / operator |
| | `runs.abandon` | **person** |
| Evidence | `evidence.read`, `evidence.refine`, `evidence.changes`, `media.sample`, `view.seek` | agent |
| Annotations | `annotations.propose_segments`, `annotations.propose_events`, `changes.diff`, `changes.validate`, `changes.edit`, `changes.undo` | agent |
| | `changes.review`, `changes.approve`, `changes.rebase`, `changes.commit`, `export.run` | **person** |
| Export | `export.plan` (the constraints of a native export; never uploads) | agent |
| Objects | `objects.strategy`, `objects.status`, `objects.detect`, `objects.propose`, `objects.plan`, `objects.run`, `objects.get`, `objects.inspect`, `objects.frame` | agent |
| | `objects.edit` | **person** |
| Tasks | `tasks.interpret`, `tasks.get`, `tasks.feedback`, `tasks.advance` | agent / operator |
| | `tasks.approve` | **person** |
| Harness | `memory.get`, `memory.search`, `cost.profile`, `knowledge.list`, `improvements.list`, `improvements.get`, `improvements.evaluate`, `improvements.revise`, `improvements.transition` (up to `awaiting_authorization`) | agent |
| | `memory.rebuild`, `knowledge.promote`, `knowledge.reject`; publishing, retaining, rolling back and resolving improvements | **person** |
| Supervision | `supervision.pending`, `supervision.feedback` | assigned teacher |
| Workspace | `gpu.status` (the local-model GPU guardian's decision) | agent |
| | `workspace.clean`, `workspace.reset` | **person** |

Human-only actions from a terminal ask for a typed confirmation: `levi agent plan approve`, `pilot review`, `changes review|commit`, `task approve`, `improvements publish|…`, `memory rebuild`, `knowledge promote`, `reset --apply`, `clean --apply`. The full generated list is in [API → Capability reference](API.md#capability-reference).

## Contracts for extension

| Concern | Entry point | Rule |
| --- | --- | --- |
| Dataset evidence | `levi/agent/formats.py:DatasetAdapter` | pin / inspect / snapshot / sample / verify / publication_state; preserve real timestamps, camera scope and source hashes |
| Annotation kinds | `AnnotationKind`, `register_annotation` | validate, apply and paths together; `[start, end)` intervals vs point events |
| Conversion | `levi/conversion/registry.py` | `InputFormat` / `OutputFormat`; never modify the source ([Conversion](CONVERSION.md)) |
| Model calls | `levi/agent/providers.py`, `levi/inference/` | typed structured output; capabilities declared, never inferred |
| Business tools | `levi/agent/capabilities.py:SPECS` | one schema, permission and side-effect registry for UI, REST and MCP |
| Harness | `levi/harness/` | closure, ledger, memory, cost, improvements, teaching, grading, tasks |
| Storage | `levi/agent/store.py` | immutable directories plus one SQLite transaction for the active revision pointer and idempotency receipt |

Proposal contract: episode, subtask id, attempt, `[start, end)` or point, outcome (`success` / `failure` / `unknown`), evidence ids, evidence note, uncertainty. A subtask outcome never implies the episode's outcome. Evidence rows record dataset time, camera, decoded video time and frame, mapping error and a content digest; model results are cached by a key covering provider, task policy, skills, input manifest, evidence and the learner brief. Budgets for calls, tokens, seconds and snapshot bytes are checked before each request; a provider that overspends after the fact stops the run rather than being trusted.

## Limits

- Evidence is sampled. Dense refinement looks where it is pointed; it cannot prove that no short event happened elsewhere.
- Real-model annotation quality is measured per dataset, not claimed: automated tests use fixtures, stub providers and simulated feedback. The local learner loop has been set up and its first interpretations graded; its annotation quality is not yet established.
- MCP is stdio only; no Streamable HTTP MCP, multi-tenant deployment, cross-camera identity, amodal masks or autonomous commit.
- External agents' output tokens are known only from their own report.
- `objects.detect` measures colour-coherent regions; it does not recognise objects.
