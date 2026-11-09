# Annotation evaluation records

LEVI records how well and how fast a person or an agent annotated subtasks, in one format, so the two can be compared line by line. 中文：[EVALUATION.zh-CN.md](EVALUATION.zh-CN.md). The last section describes the teacher / harness / direct comparison table of a teacher-student round.

| Who | When the record is written | File |
| --- | --- | --- |
| A person | **Stop recording** in the annotation view | `<workspace>/eval/<dataset>_human_<YYYYmmddTHH>.md` |
| An agent | A committed subtask-annotation run that covers **every episode** of its dataset (such as "annotate all of `<dataset>` with subtasks") | `<workspace>/eval/<dataset>_agent_<YYYYmmddTHH>_<driver>.md` |
| An agent, on request | `uv run levi agent eval <run-id>` for any other committed subtask-annotation run (a batch, a subset) | same |

The hour is the completion time in UTC, the same clock that run ids use. A second record for the same file name in the same hour is appended as a new section and never replaces the first. `driver` records who did the work:

| driver | Meaning |
| --- | --- |
| `external-mcp` | An external agent (Claude Code, Codex, …) working through LEVI's MCP tools |
| `external-pilot` | An external agent that LEVI runs as a managed Pilot |
| `api` | An online model called by LEVI (OpenAI-compatible endpoint) |
| `local-vlm` | A local model on Ollama or a local OpenAI-compatible server (`openai-local`, e.g. vLLM), working alone |
| `local-vlm-teacher` | A local model whose every phase an external teacher reviewed |
| `native-external`, `native-local-vlm`, `native-local-vlm-teacher` | The same annotators working **without LEVI**; their finished annotation is brought in afterwards |

To compare work done without LEVI on equal terms, import it through the ordinary path: plan an external run with `imported_from` set to one of the `native-*` drivers, prepare evidence, stage the finished segments with `annotations.propose_segments` (LEVI cites the frames it prepared), pass the gates and commit. The run's folder under `outputs/LEVI/workbench/agent/datasets/<dataset>/` then holds the same artifacts as an agent run, and the record is written at commit with that driver. Report the original annotator's tokens with `runs.report_usage` before committing; the import itself costs no annotator tokens.

## Recording a person's work

In a dataset's **Annotations** tab:

1. **● Start recording.** The session is stored on the server, so a page reload or a second tab keeps it.
2. Annotate. With a **subtask vocabulary**, pick the subtask and its outcome for each span; the description can be left empty.
3. When an episode is done, click **Confirm episode complete**. Unsaved edits are saved first. Only confirmed episodes count.
4. **■ Stop recording.** The summary shows the duration, the confirmed episodes, the subtask count and the coverage, plus the record's path. **Discard** ends a session without writing a record.

The subtask vocabulary is set per dataset in **Subtask vocabulary**. Set it before planning agent runs on the dataset: it is stored with the dataset's annotations, so changing it while a run is active changes that run's baseline and the run's proposals are refused. Enter one line per subtask: `id | label | definition`. If the last agent plan on the dataset defined subtasks, the editor offers them as a starting point. With a vocabulary, a human span carries the same `levi.subtask_id` and `levi.outcome` as an agent's segment. Without one, the editor works as before (free-text subtasks).

## What a record contains

Sections 1–6 are the same for people and agents. Both are measured from the subtask atoms in the dataset's annotation files, by `levi/eval/metrics.py`:

1. **Overview**: the dataset, the annotator, the start and end times, and how episodes were counted.
2. **Completion**: episodes confirmed (for an agent: episodes committed by the run), the subtask count, subtasks per episode, and the video seconds covered.
3. **Coverage**: second-level and frame-level coverage overall and per episode (mean, median and minimum), episodes at least 95 % covered, and gaps longer than 0.2 s. A span covers `[timestamp, to)`; without `to` it runs to the next subtask or to the last frame.
4. **Semantic richness**:
   - distinct labels;
   - share tagged with a vocabulary id, and vocabulary terms used;
   - share with an outcome, and the outcome distribution;
   - share with a description, words per description, share of distinct descriptions, type/token ratio;
   - descriptions that only repeat the label.
5. **Correctness**:
   - structural validity: empty or out-of-bounds spans, overlaps, ids outside the vocabulary;
   - agreement with the **same-content copies** of the dataset: segment F1 (same subtask, IoU ≥ 0.3), outcome agreement, time accuracy (share of the reference's annotated time given the same subtask, and the same subtask and outcome), boundary error. Every measure is task-agnostic: none depends on the vocabulary, the task or the frame rate. Copies are other catalog datasets that share this dataset's name stem (`<base>_human`, `<base>_agentVLM`, …) and have the same episodes and frame counts;
   - the three least-covered episodes, and a blank line for a tester's comments.
6. **Efficiency**: duration, time per episode, subtasks per minute, and video seconds annotated per minute.

An agent's record adds **7. Agent efficiency and cost**:

- **External tokens**: compute that is not on this machine. For `api` this is LEVI's metering. For external agents it is their own report, or else LEVI's measured lower bound (what it delivered to them). For `local-vlm-teacher` it is the teacher's tokens.
- **Local VLM tokens and model time** (this machine's GPU only).
- The tokens LEVI delivered to the agent, and the tool calls and their time.
- Model requests and cache hits.
- Teacher decisions, and the human gates passed (plan approval, pilot review, commit).

Every number is also in a JSON block at the end of the file, so records can be aggregated later.

## Comparing modes

To compare annotators on the same data, copy the dataset once per mode under the workspace (for example `<base>_human`, `<base>_agentCode`, `<base>_agentVLM`, `<base>_agentMix`). LEVI registers each copy, and each record reports its agreement with the other copies' annotations of the same episodes.

## Teacher, harness and direct: the three-arm round

The teacher-student method runs every round three ways on the same problems, with the same decoding and the same scoring, and compares them in one table (`levi/eval/compare.py`; [中文](EVALUATION.zh-CN.md)):

| Arm | Who answers |
| --- | --- |
| `teacher` | The external agent that supplies the paradigm and the experience entries. Its answer is a reference, not the gold standard |
| `local_harness` | The local VLM through LEVI's harness |
| `local_direct` | The same local VLM called directly, without LEVI |

The headline is whether `local_harness` is **not worse than the teacher** in quality by a margin declared beforehand, while being faster and spending fewer external tokens. So the table reports quality per second and quality per external token, not accuracy alone.

### What one arm records (`levi.eval.arm_record.v1`)

| Field | Meaning |
| --- | --- |
| `arm`, `task`, `model` | The arm, the task name, the model that answered |
| `split` | **Required**: `tune`, `confirm` or `other`, the data the arm ran on. A `confirm` record is held to the confirm rules (below) in every table, whatever a caller passes |
| `episode_set` or `episode_set_hash` + `episode_count` | The problems. `episode_set_hash` is the SHA-256 of the sorted, de-duplicated episode ids written as strings; if both forms are given they must agree |
| `scored_count` | Over how many of those problems `quality` was computed. It must equal `episode_count` in a table: a problem the arm was blocked on or failed stays in the denominator, scored as a failure. Needed whenever `quality` is given |
| `decoding` | The round's declared decoding, a non-empty JSON object. An external agent's real sampling cannot be observed or enforced, so the teacher arm records the declared decoding |
| `scoring` | The round's declared scoring, a JSON object that **must** state `higher_is_better` (`true`/`false`: is a larger `quality` better; `false` for an error such as MAE or P90) and `non_inferiority_margin` (the margin δ, a number of 0 or more, written before the round). Compared as written, with `0` and `0.0` the same |
| `quality`, `quality_ci`, `quality_ci_method` | The primary metric against the **gold standard** (its name belongs in `scoring`), its interval `[low, high]` and how it was computed. `null` = not measured |
| `quality_delta_ci_vs_teacher`, `quality_delta_ci_method` | The interval `[low, high]` of the **paired difference** (this arm's quality minus the teacher's, over the same problems) and how it was computed. It is an aggregate, computed by whoever can see the problems one by one; the table only reads it. Not on the teacher arm |
| `agreement_with_teacher` | Share of the teacher's answers this arm reproduces, 0 to 1. Not set on the teacher arm |
| `wall_s`, `wall_s_basis` | Wall-clock seconds (greater than 0) and whether they include queueing: `includes_queue` (a sub-agent's duration does) or `excludes_queue`. `null` = not stated |
| `local_tokens` | Tokens spent on this machine's GPU |
| `external_tokens` | `input_new`, `output`, `cache_read`, `cache_write` (the fields of `levi.aeri.external_report.v1`), kept apart. `null` for the whole object or for one field = unknown; `0` = measured zero |
| `external_tokens_exact` | `true` = the counts are complete, `false` = lower bounds (see below), `null` = not stated. Needs `external_tokens` |
| `external_tokens_source` | Where the external numbers came from (one line) |
| `blocked` | What blocked the arm, if anything (one line) |
| `per_episode` | Episode id to score. The only per-item field; not allowed in a confirm record |

A record with a field not in this table is refused.

### One command

```bash
python -m levi.eval.compare teacher.json harness.json direct.json            # Markdown
python -m levi.eval.compare teacher.json harness.json direct.json --format json
python -m levi.eval.compare ... --confirm              # assert: every record is split=confirm
python -m levi.eval.compare ... --verdict-basis fresh  # the token basis registered beforehand
```

Each file holds a record, a list of records, or `{"records": [...]}`. The command reads those files and prints; it starts no service, uses no GPU and opens no port. From Python: `compare.compare(records, assert_confirm=False, verdict_basis="both")` returns a `Table` with `to_markdown()` and `to_json()`. There is no `confirm` switch to forget: whether a table is a confirm table follows the records' `split`, and `--confirm` / `assert_confirm` only adds the demand that every record is one.

The Markdown has three tables with one row per arm (quality and speed; local and external tokens; differences to the teacher and to the direct arm) and a headline for `local_harness` against the teacher. The JSON has one row per arm with every column.

### Rules the table enforces

- **Same problems, same conditions.** Records whose `task`, episode-set hash, episode count, `decoding` or `scoring` differ are refused, and the message names every difference; for `decoding` and `scoring` it names the first key that differs and each arm's value. A `local_direct` record must have the same `model` as `local_harness` (the direct arm is the same model without LEVI). Every arm must have `scored_count` equal to `episode_count`. Two records for one arm, or records of different splits, are refused.
- **Unknown is not zero.** A measurement that is `null` is printed `未知` and every ratio or difference that needs it is `未知`. A ratio with a denominator of exactly 0 (a local arm that spent no external tokens) is `不适用`, not `未知`; so is an error divided by seconds or tokens (when smaller is better, such ratios are not rates of merit).
- **Quality is a non-inferiority call on an interval.** A confirm round has about six problems, and one flipped problem moves a rate by 0.14 to 0.17, so two point estimates say almost nothing. With the margin δ and direction from `scoring` and the harness record's `quality_delta_ci_vs_teacher` (oriented so that larger is better; for an error it is turned around): the answer is **yes** if the interval's worse end is within δ (lower end >= −δ), **no** if even its better end is beyond δ (upper end < −δ), `未定` (undetermined) if the interval covers the margin, and `未知` if there is no interval. The point estimate is a side line (`quality_point_not_worse`) that never enters the "all three" line. A table refuses an interval that does not contain its own point difference (usually a reversed sign).
- **External tokens: two totals side by side.** `fresh` = `input_new + output + cache_write` (what the external model newly read or wrote) and `all` = the four fields (with `cache_read`, the cheap re-reading of a cached prefix). Each has its own quality per 1,000 tokens and its own difference to the teacher, and is unknown as soon as one of its fields is. "Fewer external tokens" is answered on both: yes only if both say yes, no only if both say no, `未知` otherwise, and `fewer_external_tokens_basis_dependent` says when the two disagree (a student that re-reads a large cached prefix is fewer on one and more on the other). If a basis was registered before the round, `verdict_basis="fresh"` or `"all"` decides on that one and the table names it; both totals stay in the table.
- **A lower bound is not a count.** `external_tokens_exact: false` marks the counts as lower bounds: the table writes `≥N`, quality per token as `≤x` and differences containing one as `（含下限）`. A student arm with a lower bound is never called "fewer" (unknown). With a lower-bound teacher, a student below that bound is "fewer"; above it the answer is unknown, never "no".
- **Time is compared like with like.** A difference of times and "faster" are only given between arms of the same stated `wall_s_basis`; a different or unstated basis makes them unknown, and the table says why. A sub-agent's duration, as measured in external-agent rounds, includes queueing; a local call's time need not.
- **The direct arm is part of the round.** Without `local_direct` the table is built, but its headline is marked incomplete and its "all three" line is never yes (a no stays a no).
- **Confirm stays blind.** A record with `split: "confirm"` may carry aggregate numbers and short codes only: no `per_episode`, no explicit `episode_set` (give the hash and the count), `task`, `model`, `quality_ci_method`, `quality_delta_ci_method`, `blocked` and `external_tokens_source` each one code (at most 64 characters of `A-Za-z0-9_.:+-`), and `decoding` and `scoring` each a flat object of at most 16 entries and 256 characters whose keys are codes and whose values are codes, numbers or booleans. The check runs before any difference is reported, and error messages never repeat the content they refuse. What this cannot do is judge intent: a caller who writes an episode-specific word as a "code" (`ep7_flipped`) gets one short code through, but never a score list, an id list or a sentence. Confirm episodes' contents and labels are not shown to the teacher or the coordinator.

### External tokens from a Claude session (`levi/eval/tokens.py`)

The teacher's external tokens come from its own session transcript. A Claude session file writes one assistant message as several lines that share a `message.id`, and the usage on them grows line by line. **Each message id counts once, with its last usage record**; counting the first line under-counts a message several times over (measured on one sub-agent transcript: output 16 on the first line, 415 on the last).

```bash
python -m levi.eval.tokens session.jsonl [more.jsonl ...] --since 2026-11-06T09:00:00Z --until 2026-11-06T10:00:00Z
```

- **You name the files.** The code lists no directory, expands no glob and looks into no home folder, so it cannot sweep up sessions that are not part of the round. Pass resumed sessions oldest first (a later copy of a message replaces an earlier one).
- A message belongs to the window by the time of its first line, and then counts with its final usage.
- **`exact` speaks for the files given, not for everything the round cost.** A teacher that answers through sub-agents leaves a result line (`type: user` with a `toolUseResult` that names an `agentId`) in the main session and the sub-agent's own messages in its own file (`<session>/subagents/agent-<id>.jsonl`). Pass those files too: a result line whose sub-agent file was not given is counted (`subagent_results`, `subagents_uncovered`), its aggregate usage is not added (the sub-agent's file holds the per-message counts, and adding both would count twice), `exact` is false and `source` says so.
- Lines that cannot be parsed, a half-written last line (`truncated_tail`), assistant lines with neither `message.id` nor `uuid`, messages keyed by `uuid` because they have no `message.id` (`uuid_fallback`; lines of one message then count apart), messages without a valid time (when a window is set) and usage fields that are missing (`usage: {}` lacks all four) or not a count are **counted and reported**, never dropped silently. `exact` is false then and the totals are lower bounds (`reasons` lists the codes). A field that no counted message carried is `null`, not 0.
- `Usage.record_fields()` returns the three record fields to fill: `external_tokens`, `external_tokens_exact` and `external_tokens_source` (a sentence of at most 200 characters). For a confirm record use `record_fields(confirm=True)`: the sentence has spaces and punctuation that a confirm record refuses, so `source_code` (`claude_jsonl.last_usage:exact`, `:lower_bound`, `:not_exact` or `:nothing_counted`) stands in for it.

### Monitoring external tokens (`levi/eval/token_monitor.py`)

External tokens have no cap, but they are **monitored and counted**. `token_monitor` builds on `levi.eval.tokens` (same last-usage-per-id rule, window rule and sub-agent result-line rule) and adds a per-message list, two totals, a per-task roll-up and an alert.

```bash
python -m levi.eval.token_monitor --task jobA=main.jsonl --task jobA=subagents/agent-x.jsonl --task jobB=other.jsonl \
  --estimate jobA=200000 --since 2026-11-06T09:00:00Z --format markdown
python -m levi.eval.token_monitor --selftest
```

- **You name every file and tag it with a task label** (`LABEL=PATH`, repeat for several files of one task, oldest first). Nothing is listed or globbed. One file under two labels is refused (it would count twice). A label is `[A-Za-z0-9_.:-]`, at most 64 characters.
- **Two totals side by side**: `fresh` = `input_new + output + cache_write` and `with_cache` = the four fields (the definitions of the table above). Both are given per message, per task and in total.
- **Unknown is not 0.** A field no counted message carried is `null`/`unknown`, and a total that needs it is too. A message that lacks a field is counted in `missing_fields` and makes `exact` false (the totals are then lower bounds).
- **Alert**: `--estimate LABEL=N` (an estimate in `fresh` tokens). A task whose `fresh` total is **more than 2 x N** is flagged (`alert_state: over_2x`; a lower bound above the line still alerts). Without an estimate there is no alert and the output says `no_estimate`; if `fresh` is unknown it says `fresh_unknown`.
- **Counts only.** The JSON (`levi.eval.token_monitor.v1`) and the Markdown hold counts, message ids, times, model names and reason codes; no transcript text and no file path.
- `--selftest` checks every rule above on hand-written synthetic transcripts (values worked out by hand).
- **Not checked against a real transcript**: the sub-agent result-line handling is `tokens.py`'s. Whether a finished sub-agent's notification puts its usage into the main transcript has not been confirmed; an async launch line (`toolUseResult` with `agentId`, no `totalTokens`) is counted as a result line, and the sub-agent's own file must be passed to make the count exact.

### Not done yet

There is no adapter yet from the existing eval records, the `levi-vlm-eval` results or the AERI queue's `tokens.jsonl` to `levi.eval.arm_record.v1`; a round builds its records itself. The teacher arm's decoding is declared, not enforced. The table does not compute the paired interval (it cannot see the problems one by one) and does not check that δ was written before the round. A real Claude transcript's sub-agent result line was not read for this module; its layout (`toolUseResult.agentId`) follows the AERI token tool and its tests use hand-written fixtures.
