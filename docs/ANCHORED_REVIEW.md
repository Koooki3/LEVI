# Anchored review

A release-anchored review (anchored review for short) judges an episode at the moments the robot's own signals mark, not on frames spread evenly over it. At every recorded event — by default each time the gripper opens — a local model sees a few frames per camera at fixed offsets and answers one narrow question with a handful of enum fields. A rule over the answers says whether the event counts; a rule over the events gives the episode's outcome. The model never writes a timestamp, so an annotation's timing error can neither hide an event nor invent one.

It is a review run like any other: plan, approval, pilot, the review queue and a person's commit. What differs is where the frames come from and what the model is asked.

- [When to use it](#when-to-use-it)
- [Plan one](#plan-one)
- [The spec](#the-spec)
- [Start check and vetoes](#start-check-and-vetoes)
- [Anchors](#anchors)
- [What a run keeps](#what-a-run-keeps)
- [Reading the results](#reading-the-results)
- [Measured](#measured)
- [Limits](#limits)

## When to use it

When success is decided at a few robot actions whose moment the data records — a release, a grasp, a button press — and each can be judged from frames around it. Evenly spread samples show each such moment at most once, often not at all; an anchored review looks closely at every one of them, with the question the task needs.

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
  "workflow": {"kind": "review", "anchored": {"spec": "plates-release"}}
}
```

The plan resolves the spec and freezes it, so approving the plan approves the question, the frames and the rules. The plan's cameras must include every camera the spec shows (the plan asks for any that are missing). An anchored review runs on a bound local model — an `ollama` or `openai-local` profile — without teacher supervision. It makes one request per event; the plan's `max_calls` (at most 1000) must cover them.

`anchored.specs` lists the built-in specs, each with its title (the name people see), its id (what a plan names) and any former ids that still name it:

| Spec | Id | Task | Anchor | Frames |
| --- | --- | --- | --- | --- |
| Plates release-review rules | `plates-release` (formerly `plates-release-ar2`, still accepted) | Stack plates by colour (pink, white; green is a distractor) | gripper opens | side `view1` at −25, −15, −8, −3, −1, +4, +12 frames; wrist `hand` at −15, −6, −1, +4 |
| Plates release-review rules, rule set 3 (with start check) — `status: candidate` | `plates-release-3` | As `plates-release`, plus a [start check](#start-check-and-vetoes): a colour already stacked at the start, or with fewer than two plates, is not required | gripper opens | as `plates-release`; start check: side `view1` at the first frame |

`anchored.specs` gives each spec a `status`: `stable`, or `candidate` for a spec still being validated (`plates-release-3`). Stable specs are listed first; a candidate is never a default — an agent plans it only when a person names it — and a plan that freezes one says so in `plan.anchored_spec.status`.

A plan or script that names a former id gets the same spec. Plans approved before a rename keep the spec they froze, id included, and their results still show the spec's title.

## The spec

A spec is JSON (`levi/agent/anchored_specs/<id>.json` for the built-in ones):

| Field | Meaning |
| --- | --- |
| `id`, `version`, `description` | Identity; the description says what the spec was validated on. |
| `status` | `stable` (the default, left out of the frozen form) or `candidate`: still being validated, listed and plannable when named, never a default. |
| `title` | Optional display name per language, e.g. `{"en": "Plates release-review rules", "zh": "plates 释放复核规则"}`. The viewer and the outcome proposal show it; the id is shown only when a spec has no title. |
| `aliases` | Built-in specs only: former ids that still resolve to this spec. |
| `anchor` | `{"signal": "gripper", "event": "open" \| "close" \| "end"}`, optionally `column` (a float vector column), `dimension` (its dimension name) and `open_level` (`high`, the default, or `low` for a channel that records closure). The event `end` is the episode's last frame: exactly one event, no gripper channel read, `column`, `dimension` and `open_level` unused; it goes with `episode.rule` `final_state` and only with it. |
| `views` | Per camera: a `role`, the `camera` key and `offsets` in frames or `offsets_seconds`, counted from the anchor event. Seconds are converted with the dataset's fps when the episode's table is evenly sampled (every row within 1 % of a frame of `timestamp[0] + row / fps`, as in every LEVI conversion); on an uneven table (dropped frames, a jittery clock) each offset in seconds shows the row whose timestamp is nearest the asked time (the earlier row on a tie), and the record says `"offset_timing": "timestamps"`. Offsets in frames are always rows. Offsets are clamped to the episode. The images are sent in this order, native size, PNG. The spec's own views are always at the anchor; `"at": "start"` / `"at": "end"` (offsets from the episode's first / last frame) belong to the start check's views, and a veto's own views may use them too. |
| `question` | The whole instruction; no system prompt, skills or evidence ledger are added. |
| `fields` | Ordered answer fields, each with its `enum`. The server decodes them in this order, all required. |
| `valid_when` | Conditions `{"field", "in": [...]}` or `{"field", "not_in": [...]}`; an event is valid when all hold. |
| `unknown_values` | Answers that mean "cannot tell" (default `["unclear"]`). |
| `episode` | `{"label_field", "require_labels": [...]}`: success when every label has a valid event; or `{"min_valid": n}`, optionally with `"rule": "last_valid_not_regrasped"` (also needs the gripper not to close again after the last valid event; `basis` gets `rule`, `last_valid_frame`, `closes_after_last_valid`, `require_place`, and the record gets `closes`, the frames where the gripper closes) and `"require_place": true` (the live service also requires the episode's last `place` time segment not to be a failure or unknown; the review cannot see time segments). The default `"rule": "any_valid"` is left out of the frozen form. `"rule": "final_state"` (with `anchor.event` `end`; no `min_valid` above 1, no `require_place`) judges the one end-of-episode event: success when it is valid, failure when it is contradicted, and a failure that is undecided when it is unknown (`basis` gets `rule`, `valid_events`, `min_valid` and `final_reading`: `supported`, `contradicted` or `unknown`; `anchored.undecided` is true when it is unknown); the record has no `closes`. The live service's `generic-final.v1.json` is such a spec; see [Final-state judgement](LIVE.md#final-state-judgement-candidate). See [Live annotation service](LIVE.md#terminal-aware-verdict-candidate). |
| `max_output_tokens` | The answer's allowance (default 200). |
| `start` | Optional [start check](#start-check-and-vetoes): one question per episode on frames at its start, whose answer can waive required labels. |
| `vetoes` | Optional [vetoes](#start-check-and-vetoes): rules any event can break, each read from the event's answer or asked as its own question. |

Each condition of an event reads as **supported** (it holds), **contradicted** (it fails on a definite answer) or **unknown** (it fails on an unknown value). The event is valid when every condition is supported, contradicted when any is contradicted, unknown otherwise. A label the episode needs that has no valid event but an unknown one is named in the outcome proposal's `uncertainty`, so the reviewer sees where to look.

## Start check and vetoes

Both are declared in the spec, as data; a spec without them runs exactly as before (same requests, record and outcome, and the plan freezes the same spec), which a regression test checks byte for byte on recorded answers.

**Start check** (`start`): one more question, asked once per episode before the events, on views `at` the episode's `start` (or `end`). It has its own `question`, `fields` and `max_output_tokens`, and a list `waive` of `{"label", "when": [conditions over its answer]}` (each label at most once). A required label (`episode.require_labels`) is waived — the episode does not need a valid event for it — when every condition holds; when one reads unknown, the label stays required and, if it has no valid event, is named undecided. For plates: a colour already stacked at the start, or with fewer than two plates, is not required.

The record's `basis` splits the waivers:

- `waived_labels`: waivers the outcome rests on (the label has no valid event). The outcome proposal cites the start check's frames for them.
- `redundant_waivers`: the label has a valid event anyway; the waiver changed nothing.
- `contested_waivers`: waivers in `waived_labels` whose label does have events, none valid (contradicted or unknown) — the episode handled that label, so the start check's answer may be wrong. A success that rests on one is **undecided**: it is named in the proposal's `uncertainty`, `anchored.undecided` is true and the training manifest's `anchored_undecided` is true. The outcome itself stays success; a person decides.

```json
"start": {
  "views": [{"role": "start_side", "camera": "observation.images.view1", "offsets": [0], "at": "start"}],
  "question": "... For pink and for white: two_or_more_apart, already_stacked, one_or_none or unclear ...",
  "fields": [{"name": "pink", "enum": ["two_or_more_apart", "already_stacked", "one_or_none", "unclear"]}, ...],
  "waive": [{"label": "pink", "when": [{"field": "pink", "in": ["already_stacked", "one_or_none"]}]}, ...]
}
```

**Vetoes** (`vetoes`): a list of rules that any single event can break. Each has an `id`, an optional `title` (per language, checked like the spec's; kept in the record, not yet shown), and:

| Field | Meaning |
| --- | --- |
| `ask_when` | Conditions over the event's own answer; the veto is read at events where none is contradicted (every event when empty). |
| `question`, `fields`, `views`, `max_output_tokens` | Optional: the veto's own question, asked at the event after the spec's question. `views` default to the spec's (the same images, so a server's prefix cache serves them). Without a question, the veto reads the event's own answer, and `views` or `max_output_tokens` are refused. |
| `veto_when` | Conditions over the veto's answer (or the event's). All supported: **confirmed**; one contradicted: **cleared**; otherwise **undecided**. |
| `effect` | `episode` (default): a confirmed veto at any event makes the outcome failure. `event`: it only makes its event invalid (contradicted); an undecided one makes a valid event unknown. |

An undecided `episode` veto does not change the outcome: like an undecided label, it is named in the proposal's `uncertainty` (for a success) and in the record's `basis.undecided_vetoes`, so the reviewer sees where to look; the training manifest's `anchored_undecided` counts it. For screws, "a target screw dropped into a compartment that already held screws or nuts" is a veto with its own question, and "the robot dropped a distractor into an empty compartment" a veto read from the event's answer:

```json
"vetoes": [
  {"id": "occupied_compartment",
   "ask_when": [{"field": "object", "in": ["short_silver_screw"]}],
   "question": "... does that compartment hold black screws, nuts or other hardware ...?",
   "fields": [{"name": "compartment_had_hardware", "enum": ["yes", "no", "unclear"]}],
   "veto_when": [{"field": "compartment_had_hardware", "in": ["yes"]}]},
  {"id": "distractor_in_empty_compartment",
   "veto_when": [{"field": "object", "in": ["black_screw", "long_silver_screw", "hex_key"]},
                 {"field": "landed_in", "in": ["empty_compartment"]}]}
]
```

Each start check and veto question is one more request with the same safeguards as the spec's own (budget, cache, an answer outside its fields sets the episode aside); the plan's estimate names them, and with a start check its `minimum_requests` is one per episode.

**What an outcome is undecided on** (`anchored.undecided`, the manifest's `anchored_undecided`): a required label with no valid event but an unknown one, or with an unknown waiver (for either outcome); for a success, also an undecided `episode` veto, a contested waiver or an input its rule needed and did not have (`basis.missing_inputs`: the closes of a record made without them, or `place` under `require_place`, which only the live service can apply).

**Cited frames.** The outcome proposal cites, in order: for an event whose `episode` veto was confirmed, the frames that veto's own question looked at, then the event's frames nearest the anchor; the episode's last frame when it has no event; the start check's frames when a waiver decided the outcome; then the valid events' frames (at most 32).

## Anchors

Like the evidence [signals](AGENTS.md#evidence-and-refinement), anchors are read from what the dataset declares: a float vector column and a dimension whose name says gripper (`grip`, `finger`, `claw`, `jaw`). The recorded state (`observation.*`) is used before `action` unless the spec names a column. Crossings are found with hysteresis on the channel's range (the dataset's `meta/stats.json` range, else the episode's); the level an episode starts at is its initial state.

| Dataset | What the anchor is |
| --- | --- |
| Raw robot capture (browsed through its view) | The view's gripper dimension is the per-frame gripper command (`open=1`, `close=0`, from `gripper_state.csv` `last_gripper_command`), so anchors are the command's transitions to the frame. On the frozen test set (60 plates episodes) they equal the transitions of the CSV itself in all 60 episodes. |
| LeRobot dataset with a binary gripper command | The command's transitions. With `action_mode: next_state` conversions, `action` leads the state by one frame; the default (state) matches the capture. |
| LeRobot dataset with a measured aperture | Crossings of 35 % / 65 % of the range. They lag the command by the gripper's travel; set offsets accordingly, or anchor on the command column (`anchor.column: "action"`). |
| Any dataset, anchor `end` | The episode's last frame, one event; nothing is read from the gripper, so no gripper dimension is needed. Offsets count back from it and are clamped to the episode, so an episode shorter than the offsets shows its first frame in their place. |
| No gripper-named dimension (anchor `open` or `close`) | Not supported: the run blocks on the first episode and names the missing channel. Name one with `anchor.column` / `anchor.dimension`. |

## What a run keeps

Per episode, beside the run and in the agent store (`anchored` records, removed with the run by `reset`):

- `episode_NNNNNN-anchored.json`: the spec's id and title, the channel read, every event's frame and time, its answers, the per-condition reading, its verdict and validity, the frames shown (camera, offset, frame, evidence id; the offset is the view's nominal offset in frames, `round(seconds × fps)` for a view in seconds, also where an uneven table showed another row) and its tokens and time; with vetoes, each veto's verdict at the event (`confirmed`, `cleared`, `undecided` or `not_asked`) with its answer and frames; with a start check, its answer, each waiver's reading and its frames (`start`); the episode's outcome and what it rests on (valid labels, missing, undecided, and when used, waived labels, confirmed and undecided vetoes); `offset_timing: "timestamps"` when views in seconds were looked up by timestamp on an uneven table (an even table's record has no such key).
- The evidence ledger (`episode_NNNNNN-observations.json`) and the frames as lossless PNG under `evidence/` (removed by `levi agent clean`, rebuilt on demand).
- One `outcome` proposal in the run's review draft: success or failure, the answers of each event in its text, citing the frames nearest each event (valid events first). Committing it writes the episode's outcome label, which the episode list shows and exports carry.

Each question gets the same safeguards as a model phase: the budget is reserved and settled, answers are cached (a resumed run does not ask again), an answer outside the spec sets its episode aside with its tokens settled, the GPU guardian can hold the run, and pause and cancel cut the request in flight.

## Reading the results

- `anchored.get` — `{"repo_id"}` or `{"run_id"}`: the newest anchored review's per-episode outcome, event and valid counts, requests, tokens and time; with `"episode": N`, that episode's record and the spec's fields and rules. Both carry the spec's `id` and `title`.
- `GET /api/annotation/anchored/summary` and `GET /api/annotation/anchored/episodes/{N}` through the web UI (`/api/anchored/…` on the annotation service; `repo_id` or `local_path`, optional `run_id`) — the same for the viewer.
- In the episode viewer, the annotations timeline shows an **ANCHORED REVIEW** row headed by the spec's title (its id on hover): one marker per event at its frame, green when valid, red when contradicted, amber when unknown; hovering shows the answers and each condition, clicking seeks to the event. The row does not yet show the start check or veto verdicts: a marker is coloured by the event's verdict after its `effect: event` vetoes, and hovering shows only the spec's own answer. Read them in the outcome proposal (its text, `evidence_note` and `uncertainty`) or with `anchored.get`.

## Measured

The plates release-review rules (`plates-release`) reproduce the external release-anchored review script accepted with the current best configuration v1.1 (rule set 2 of the anchored review): the same anchors, frames, pixels (PNG of the full-resolution frames), question, schema and rules, thinking off, greedy decoding on the server. In one server session on the frozen test set (60 plates episodes), LEVI and the external script gave identical answers on all 205 events and identical outcomes on all 60 episodes, with the same tokens; LEVI took 369 s against the script's 303–320 s. See the [validation record](VALIDATION.md#anchored-review).

The candidate rule set 3 (`plates-release-3`) was checked on development data only (63 human-judged plates episodes: 48 of the rollouts_recap pool, the diagnostic set of 15; one server session, greedy). Against rule set 2 on the same answers: agreement with people 0.810 → 0.921, success recall 0.816 → 0.959, false successes unchanged at 3 of 14 failures. The start check called two separate white plates a single one in 11 of about 57 episodes; wherever those white plates are never stacked, that waiver would give a false success, so the candidate is not the default until a held-out check.

## Limits

- One anchor signal: gripper crossings. Other events (contact, a button, a height turn) are not yet anchors, apart from the episode's end (`end`), which judges only the last frames and sees nothing of the steps before them.
- Offsets are fixed per spec; a gripper that moves much faster or slower than the one a spec was written for needs its own offsets.
- The episode rule is a set cover or a count, narrowed by a start check and broken by vetoes. Order constraints (this before that) are not expressed, and a later event cannot undo an earlier `episode` veto (use `effect: event` for a fault a later event can correct).
- Greedy answers are deterministic within one server session; a restarted server can change a few answers.
