# Built-in knowledge

LEVI stores what it learns in two layers:

| Layer | Where | Scope | Who reads it |
| --- | --- | --- | --- |
| Local memory | `.state/outputs/LEVI/workbench/memory/*.json` (git-ignored) | One workspace: a dataset's committed facts, teacher notes, lessons | The next task on that dataset |
| Built-in knowledge | `levi/knowledge/<topic>.md` (in the repository) | Rules that hold for any dataset | Every installation, through the code |

The built-in topics:

- `annotation`: every model LEVI runs on an annotation task receives these rules (`levi_rules` in its brief). External agents get them from `workspace.get_context` (`annotation_rules`).
- `interpretation`: the model that turns a sentence into a task spec receives these.
- `harness`: a reference for harness and kernel work. No model prompt includes it.

## How local memory becomes built-in knowledge

1. Every closed run refreshes the candidate list (`workbench/knowledge/candidates.json`). The candidates are the teacher notes and retained lessons in local memory that are not already built in.
2. A candidate is **ready** when it recurs (taught more than once, or on more than one dataset) and names no episode, time or range. The check is deliberately strict, because one dataset's facts must not become every dataset's rules.
3. A person decides:

   ```bash
   uv run levi agent knowledge list                  # built-in entries and open candidates
   uv run levi agent knowledge refresh               # recompute candidates now
   uv run levi agent knowledge promote candidate-004 --text "…"   # rewrite into a general rule
   uv run levi agent knowledge reject candidate-005
   ```

   Promoting appends `- **<topic>-NNN** · <text> _(from: <source>, <date>)_` to the topic file. Promoting refuses text that still names episodes or times.
4. The entry reaches other installations with the next commit, where code review applies.
5. `uv run levi docs sync` refreshes the index below, and `levi docs check` fails until it is refreshed.

Agents can read the candidates (`knowledge.list`). Only a person can promote or reject one (`knowledge.promote`, `knowledge.reject`).

## Index

<!-- levi:generated knowledge -->
### annotation (12)

- **annotation-001** · Cover the whole episode with consecutive intervals, from the first to the last frame; intervals in one layer never overlap, and each ends after it starts.
- **annotation-002** · A place, transport or stacking step succeeds only when its goal condition is visibly met (for example the object rests on a target of the required kind); name what was placed and what is under it before writing the outcome.
- **annotation-003** · When the recording ends before a release or result is visible, the outcome is unknown, not success and not failure.
- **annotation-004** · Anything a human hand does is labelled `other` with outcome unknown, never as a robot subtask.
- **annotation-005** · Something not seen in coarse samples may still have happened: lifts, releases and hand entries often last well under a second. Refine that interval with dense frames before judging it a failure.
- **annotation-006** · Read times from each frame's timestamp, never from frame numbers or image order.
- **annotation-007** · An attempt is one continuous engagement with an object: it starts at contact and lasts while the effector stays on it (re-closing or adjusting the grip is the same attempt); it ends when the effector lets go and moves away, or when the object moves with it. Give each attempt its own interval and outcome -- failure when the effector leaves without the object, unknown only when the recording does not show which -- and label the move from letting go to the next contact as a new approach; two intervals of the same subtask never meet. How finely a task is split is the task's instruction to state; boundaries alone do not settle it.
- **annotation-008** · The description (`content`) is one short sentence of what the robot does. It contains no frame numbers and no reasoning.
- **annotation-009** · Episode-level metadata outcomes and per-subtask outcomes answer different questions: an episode can contain successful subtasks and still fail. Never substitute one for the other.
- **annotation-010** · A dataset can contain demonstrations of other tasks. Judge which task a demo shows from what is on the table (the wide camera), before judging its outcome.
- **annotation-011** · Judge small-part outcomes (a screw seated in a slot) from the wrist camera's last frames; a hand or object entering at the end does not change the outcome by itself.
- **annotation-012** · `unknown` says the recording does not show the outcome, not that it was not looked at: when a coarse sheet leaves an outcome or a boundary unsettled, watch that stretch densely before writing `unknown`.

### interpretation (6)

- **interpretation-001** · An annotate step always needs an instruction: what to mark, and when an outcome counts as success, failure or unknown.
- **interpretation-002** · When the request asks for token or time statistics, `report` is `["tokens", "time"]`.
- **interpretation-003** · Choose the fewest cameras that show what must be judged: a wide camera for which task or scene, the wrist camera for small parts. Each extra camera doubles the images and the cost.
- **interpretation-004** · "Data quality check" is a `quality` step and needs no episodes. Add one only when it is asked for.
- **interpretation-005** · Episode-level questions (which task, did it succeed) use workflow `review`; subtask intervals use `temporal`.
- **interpretation-006** · Episode indices start at 0 ("the first 10 demos" are episodes zero to nine). An explicit list such as "23, 25, 27" is copied exactly, never turned into a range.

### harness (10)

- **harness-001** · Structural mistakes by a small model (empty answers, missing fields, invented ids, rewriting a draft) are removed by narrowing the decoding schema, not by notes. Notes help with meaning and carry over to later tasks.
- **harness-002** · Put the answer before the prose: llama.cpp decodes required properties first and in order, so a required `proposals` placed before a short `summary` stops a small model from spending its output budget on narration.
- **harness-003** · Size each request to the model: learn per-image and per-character token cost from metered calls, send compact evidence rows, split long refinements into batches and grow the answer budget with the number of expected intervals.
- **harness-004** · Keeping refined boundaries within the frames a refinement was shown helps when the draft was reviewed. It locks in coarse errors when nobody reviewed the draft.
- **harness-005** · Most of an external agent's tokens are often tool-response text, not images. Keep receipts compact, and let the agent read a whole episode as one contact sheet at about 1 Hz.
- **harness-006** · Supervising a learner does not save a teacher's tokens while the teacher still looks at every episode. Savings need a high first-pass acceptance rate and sampled review.
- **harness-007** · A shared GPU needs a guardian that learns how other workloads restart and yields to them, preempts in-flight requests and resumes automatically. A fixed quiet window almost never opens.
- **harness-008** · Settle every model call's tokens whether or not its answer is valid, and keep the valid part of an invalid answer.
- **harness-009** · An external agent's cost follows the number of turns more than the images: each turn re-reads the whole context. Paging an episode in small pages, refinements that must be re-read by paging, citations the agent has to look up and contract rules found out by refusal each add turns and helper scripts. Give a whole episode in one sheet, return refined frames as their own sheet, let the harness cite what it showed, and state the proposal contract up front.
- **harness-010** · Measure an agent's tokens per request from its own transcript, count cache reads separately, and keep the operator's work (fixes, gates, retries it caused) out of the task's figure.
<!-- /levi:generated knowledge -->
