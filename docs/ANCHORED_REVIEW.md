# Anchored review

An anchored review judges an episode at the moments the robot's own signals mark, not on frames spread evenly over it. At every recorded event — by default each time the gripper opens — a local model sees a few frames per camera at fixed offsets and answers one narrow question with a handful of enum fields. A rule over the answers says whether the event counts; a rule over the events gives the episode's outcome. The model never writes a timestamp, so an annotation's timing error can neither hide an event nor invent one.

It is a review run like any other: plan, approval, pilot, the review queue and a person's commit. What differs is where the frames come from and what the model is asked.

- [When to use it](#when-to-use-it)
- [Plan one](#plan-one)
- [The spec](#the-spec)
- [Anchors](#anchors)
- [What a run keeps](#what-a-run-keeps)
- [Reading the results](#reading-the-results)
- [Measured](#measured)
- [Limits](#limits)

## When to use it

When success is decided at a few robot actions whose moment the data records — a release, a grasp, a button press — and each can be judged from frames around it. An even sample of an episode shows each such moment at most once, often not at all; an anchored review looks at every one of them, closely, with the question the task needs.

## Plan one

Give a review plan a `workflow.anchored`: a built-in spec by id, or a whole spec.

```json
{
  "repo_id": "local/stack_the_plates_of_same_color_together",
  "episodes": [0, 1, 2],
  "cameras": ["observation.images.view1", "observation.images.hand"],
  "instruction": "Release-anchored review",
  "provider": "qwen38-27b-vllm-48k-lean",
  "allow_media_egress": true,
  "workflow": {"kind": "review", "anchored": {"spec": "plates-release-ar2"}}
}
```

The plan resolves the spec and freezes it, so approving the plan approves the question, the frames and the rules. The plan's cameras must include every camera the spec shows (the plan asks for any that are missing). An anchored review runs on a bound local model — an `ollama` or `openai-local` profile — without teacher supervision. It makes one request per event; the plan's `max_calls` (at most 1000) must cover them.

`anchored.specs` lists the built-in specs:

| Spec | Task | Anchor | Frames |
| --- | --- | --- | --- |
| `plates-release-ar2` | Stack plates by colour (pink, white; green is a distractor) | gripper opens | side `view1` at −25, −15, −8, −3, −1, +4, +12 frames; wrist `hand` at −15, −6, −1, +4 |

## The spec

A spec is JSON (`levi/agent/anchored_specs/<id>.json` for the built-in ones):

| Field | Meaning |
| --- | --- |
| `id`, `version`, `description` | Identity; the description says what the spec was validated on. |
| `anchor` | `{"signal": "gripper", "event": "open" \| "close"}`, optionally `column` (a float vector column), `dimension` (its dimension name) and `open_level` (`high`, the default, or `low` for a channel that records closure). |
| `views` | Per camera: a `role`, the `camera` key and `offsets` in frames or `offsets_seconds` (converted with the dataset's fps). Offsets are clamped to the episode. The images are sent in this order, native size, PNG. |
| `question` | The whole instruction; no system prompt, skills or evidence ledger are added. |
| `fields` | Ordered answer fields, each with its `enum`. The server decodes them in this order, all required. |
| `valid_when` | Conditions `{"field", "in": [...]}` or `{"field", "not_in": [...]}`; an event is valid when all hold. |
| `unknown_values` | Answers that mean "cannot tell" (default `["unclear"]`). |
| `episode` | `{"label_field", "require_labels": [...]}`: success when every label has a valid event; or `{"min_valid": n}`. |
| `max_output_tokens` | The answer's allowance (default 200). |

Each condition of an event reads as **supported** (it holds), **contradicted** (it fails on a definite answer) or **unknown** (it fails on an unknown value). The event is valid when every condition is supported, contradicted when any is contradicted, unknown otherwise. A label the episode needs that has no valid event but an unknown one is named in the outcome proposal's `uncertainty`, so the reviewer sees where to look.

## Anchors

Anchors are read from what the dataset declares, as the evidence [signals](AGENTS.md#evidence-and-refinement) are: a float vector column and a dimension whose name says gripper (`grip`, `finger`, `claw`, `jaw`). The recorded state (`observation.*`) is used before `action` unless the spec names a column. Crossings are found with hysteresis on the channel's range (the dataset's `meta/stats.json` range, else the episode's); the level an episode starts at is its initial state.

| Dataset | What the anchor is |
| --- | --- |
| Raw robot capture (browsed through its view) | The view's gripper dimension is the per-frame gripper command (`open=1`, `close=0`, from `gripper_state.csv` `last_gripper_command`), so anchors are the command's transitions to the frame. On the frozen plates test set they equal the transitions of the CSV itself in all 60 episodes. |
| LeRobot dataset with a binary gripper command | The command's transitions. With `action_mode: next_state` conversions, `action` leads the state by one frame; the default (state) matches the capture. |
| LeRobot dataset with a measured aperture | Crossings of 35 % / 65 % of the range. They lag the command by the gripper's travel; set offsets accordingly, or anchor on the command column (`anchor.column: "action"`). |
| No gripper-named dimension | Not supported: the run blocks on the first episode and names the missing channel. Name one with `anchor.column` / `anchor.dimension`. |

## What a run keeps

Per episode, beside the run and in the agent store (`anchored` records, removed with the run by `reset`):

- `episode_NNNNNN-anchored.json`: the spec id, the channel read, every event's frame and time, its answers, the per-condition reading, its verdict and validity, the frames shown (camera, offset, frame, evidence id) and its tokens and time; the episode's outcome and what it rests on (valid labels, missing, undecided).
- The evidence ledger (`episode_NNNNNN-observations.json`) and the frames as lossless PNG under `evidence/` (removed by `levi agent clean`, rebuilt on demand).
- One `outcome` proposal in the run's review draft: success or failure, the answers of each event in its text, citing the frames nearest each event (valid events first). Committing it writes the episode's outcome label, which the episode list shows and exports carry.

Each question has a model phase's safeguards: the budget is reserved and settled, answers are cached (a resumed run does not ask again), an answer outside the spec sets its episode aside with its tokens settled, the GPU guardian can hold the run, and pause and cancel cut the request in flight.

## Reading the results

- `anchored.get` — `{"repo_id"}` or `{"run_id"}`: the newest anchored review's per-episode outcome, event and valid counts, requests, tokens and time; with `"episode": N`, that episode's record and the spec's fields and rules.
- `GET /api/anchored/summary` and `GET /api/anchored/episodes/{N}` (`repo_id` or `local_path`, optional `run_id`) — the same for the viewer.
- In the episode viewer, the annotations timeline shows an **ANCHORED REVIEW** row: one marker per event at its frame, green when valid, red when contradicted, amber when unknown; hovering shows the answers and each condition, clicking seeks to the event.

## Measured

`plates-release-ar2` reproduces the external release-anchored review accepted in milestone plates-v1.1 (AR2): the same anchors, frames, pixels (PNG of the full-resolution frames), question, schema and rules, thinking off, greedy decoding on the server. In one server session on the frozen 60-episode plates test set, LEVI and the external script gave identical answers on all 205 events and identical outcomes on all 60 episodes, with the same tokens; LEVI took 369 s against the script's 303–320 s. See the [validation record](VALIDATION.md#anchored-review).

## Limits

- One anchor signal: gripper crossings. Other events (contact, a button, a height turn) are not yet anchors.
- Offsets are fixed per spec; a gripper that moves much faster or slower than the one a spec was written for needs its own offsets.
- The episode rule is a set cover or a count. Order constraints (this before that) are not expressed.
- Greedy answers are deterministic within one server session; a restarted server can change a few answers.
